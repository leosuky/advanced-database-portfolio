# Portfolio 4: NoSQL and Advanced Data Models

PostgreSQL stays the **system of record** for The Data XI. NoSQL is added where a relational database is the wrong
shape or the wrong speed for a job. Two models are implemented on the same case study:

| Model | Product | Job it does for The Data XI |
|---|---|---|
| **Key-value / sorted set / hash** | Redis 7 | the hot, tiny, repeated reads: leaderboards, live match state, cached match pages, API rate limits |
| **Document** | MongoDB 7 | (a) the whole match page as one document; (b) raw provider payloads whose shape differs per provider |

(Graph model: see the note at the end.)

## 4a. Redis (key-value) - `redis/redis_demo.py`, output in `results/redis_demo.log`

| Redis structure | Use | Commands | Result |
|---|---|---|---|
| **Sorted set** `lb:scorers:3` | season scorers leaderboard (520 players) | `ZADD`, `ZREVRANGE`, `ZREVRANK`, `ZSCORE` | top-10 read in **0.52 ms** vs **2.31 ms** for the PostgreSQL query (median of 300 calls, same machine): **about 4x** (see note below) |
| **Hash** `live:match:951` + **pub/sub** | live minute and score, pushed to subscribers | `HSET`, `HINCRBY`, `PUBLISH` | a subscriber received both pushed events; `HINCRBY` is atomic, so two goals scored at once cannot be lost (the same lost-update lesson as Portfolio 3) |
| **String with TTL** `cache:matchpage:784` | cache-aside for the match page | `SET key value EX 30`, `GET`, `DEL` | miss 13.2 ms (queries PostgreSQL) then hit 0.68 ms; **99% hit rate** over 100 requests |
| **Counter with expiry** | API rate limit, 100 requests per 10 s | `INCR`, `EXPIRE` | 105 requests: 100 allowed, 5 blocked |

**Note on the speed-up.** These figures come from Docker Desktop on macOS, where every call from Python to either
container crosses Docker's virtual-machine network. That round trip is roughly 0.5 ms on its own, so most of Redis's
0.52 ms median is network, not Redis. An earlier run on Linux with no VM in between measured 0.109 ms vs 3.31 ms (30x).
The honest conclusion is that Redis wins clearly, but the size of the win depends on the network between the
application and the store. In production the cache sits next to the API, so the Linux figure is the closer guide.
The worst case also matters: in this run Redis's slowest call (5.25 ms) was slower than PostgreSQL's (4.6 ms), which
is network jitter, not Redis.

**The trade-off, demonstrated.** After the cache is warm, the score of match 784 was changed in PostgreSQL (0-0 to 0-1).
The cached page kept saying **0-0** until the key was deleted (`DEL` = cache invalidation on write), after which it
said 0-1. That is **eventual consistency (BASE)**: fast and available, but briefly stale. The design rule: cache things
that tolerate staleness (leaderboards, match pages), never the standings during a correction.

**Why Redis fits:** in-memory, O(log n) sorted-set operations, atomic increments, TTL built in. **Why not use it as the
only database:** no relational integrity, no ad-hoc joins, and memory-bound (all 523 keys use only 1.6 MB, but 1.14M
events would not be sensible to hold there).

## 4b. MongoDB (document) - `mongodb/`

### Model 1: `match_documents` (embedding)

One document per match, with both line-ups and the shot list embedded (abridged; the values here are illustrative, the real ones are in the loaded data):

```json
{ "_id": 951, "season": "2025/26", "matchday": 13, "kickoff": "...",
  "home": { "team_id": 3, "name": "...", "goals": 0, "xg": 1.2, "lineup": [ {"player_id": 12, "name": "...", "pos": "GK", "minutes": 90, "goals": 0, "xg": 0.0}, ... ] },
  "away": { ... },
  "shots": [ {"player_id": 87, "minute": 14, "xg": 0.08, "outcome": "saved", "x": 91.2, "y": 40.5}, ... ] }
```

*Why embed:* a match page needs all of this together, the size is bounded (22 players, about 30 shots, **5.1 KB** per
document), and one read replaces joins across five tables. *Why not embed events:* 1,000 events per match would make the
document large and unbounded, so events stay in PostgreSQL.

| Query | Pipeline / filter | Result on the simulated data |
|---|---|---|
| M1 point read | `find_one({_id: 951})` | the whole match page in one read |
| M2 fixtures of a team | `$or` on `home.team_id` / `away.team_id` + `season`, sorted by matchday | `explain()` on real MongoDB 7.0.45: `SORT <- FETCH <- IXSCAN(home.team_id_1_season_1)`, **19 documents examined for 19 returned**. The same query forced to a collection scan examined **all 1,140**. (Both took under 10 ms at this size, and the COLLSCAN even reported 0 ms: the gain is in work done, which grows with the collection, not in time on 1,140 small documents.) |
| M3 top scorers | `$match` -> `$project $concatArrays` of both line-ups -> `$unwind` -> `$group $sum` -> `$sort` -> `$limit` | top five agree with PostgreSQL (checked by player id: Ade Santos 12, Chidi Silva 11, ...) |
| M4 array query | `shots: {$elemMatch: {minute: {$gt: 90}, outcome: "goal"}}` | 114 matches with a stoppage-time goal |

### Model 2: `provider_raw_payloads` (schema-on-read)

Four providers return four different JSON shapes (`events`, `general/stats`, `shotmap`, `lineups`) and a provider can
change its format at any time (`schema_version` 1 and 2 are mixed in the data: 100 payloads on version 2). Storing the
raw payload first and parsing it later fits a document store; forcing it into tables on arrival would break each time a
provider changed. A unique index on `(match_id, provider)` keeps ingestion idempotent.

### Schema flexibility is not schema freedom
`mongodb/01_schema_validation_and_indexes.js` adds a `$jsonSchema` validator to `match_documents` (required fields, goal
counts >= 0, at most 25 players per line-up) while leaving the raw payload collection unvalidated on purpose. On the real
server an invalid insert was refused with **code 121, `Document failed validation`** (`results/mongosh_01_validation.txt`).

### Trade-offs recorded in `mongo_demo.log`
The same fact (a goal) now lives in `home.goals`, in the line-up player's `goals` and in the `shots` list. Correcting a
score means updating several places in the document, where PostgreSQL changes one `match` row (plus the derived standings).
Documents are good for read-mostly aggregates and bad for data that is corrected often. That is why corrections stay in
PostgreSQL (Portfolio 3) and the document is rebuilt from it.

### What was and was not run (important)

| Item | Status |
|---|---|
| Document construction, loading, M1 to M5 logic | first checked with the in-memory `mongomock` library, then **run on real MongoDB 7.0.45** (Docker): `results/mongo_demo.log` (its first line names the backend). Results were identical |
| `01_schema_validation_and_indexes.js` | **run in `mongosh`** on the real server: `results/mongosh_01_validation.txt`, screenshot `screenshots/p4-mongo-validator.png` |
| `02_queries_and_explain.js` | **run in `mongosh`** on the real server: `results/mongosh_02_explain.txt`, screenshot `screenshots/p4-mongo-explain.png` |
| Bugs found on the real server | `01` used `createCollection`, which fails once the demo has loaded the collection: changed to `collMod`. The rejection printed `undefined` (write errors carry `code` 121, not always `codeName`): now prints the code and message. `02` read the plan only one level deep: now walks the whole plan. None of these showed up under mongomock |

## Graph model (discussion)

Pass networks are naturally a graph: players are nodes, successful passes are weighted edges (query Q6 returns exactly
that edge list). A graph database (e.g. Neo4j, Cypher: `MATCH (a:Player)-[p:PASSED_TO]->(b:Player) WHERE p.match = 951
RETURN a, b, p.count`) would make multi-hop questions such as "possession chains of 4 or more passes" simpler than
recursive SQL. It was **not implemented**, because Q6 already answers the single-match case in 0.2 ms in PostgreSQL;
a graph store becomes worthwhile only for deep multi-hop analysis across many matches.

## Suitability summary

| Need | Best fit | Reason |
|---|---|---|
| Correctness, joins, corrections (scores, standings) | PostgreSQL | ACID, constraints |
| Fast repeated tiny reads | Redis | in-memory, sorted sets, TTL |
| One-read aggregates and variable raw payloads | MongoDB | embedding, schema-on-read |
| Multi-hop relationship analysis | graph DB (future) | traversal cost |
