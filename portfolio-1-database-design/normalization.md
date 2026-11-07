# Normalisation walk-through

This shows how the schema in `sql/01_schema.sql` was reached from the kind of flat table a data provider hands you,
step by step, and then lists the places where the design **deliberately** stops short of full normalisation.

## 0. Starting point: one flat row per player per match

This is roughly what a provider export looks like (one row per player appearance, everything repeated):

| match_id | kickoff | season | home_team | home_short | away_team | away_short | home_goals | away_goals | player_id | player_name | birth_date | nationality | team_name | position | minutes | goals | xg |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

Problems (anomalies):

* **Update anomaly**: renaming a club means editing thousands of rows; correcting a score means editing all 22+ rows of that match.
* **Insert anomaly**: you cannot record a team or a player until they have appeared in a match.
* **Delete anomaly**: deleting the only appearance of a player deletes the fact that the player exists.

Primary key of the flat table: `(match_id, player_id)`.

## 1. First normal form (1NF)

Rule: every column holds one atomic value, no repeating groups.

The flat table is *almost* 1NF already. The usual violations in provider data are lists, for example `lineup = "12,15,19,..."`
or a `shots` column that stores a JSON array. 1NF moves each list item into its own row: that is why `player_match` and `shot`
are separate tables with one row per player-appearance and per shot.

## 2. Second normal form (2NF)

Rule: 1NF, and no non-key column depends on only *part* of a composite key.

Composite key `(match_id, player_id)`. Functional dependencies found:

| Determinant | Determines | Verdict |
|---|---|---|
| `match_id` | kickoff, season, matchday, home/away teams, home/away goals | partial dependency, move out -> `match` |
| `player_id` | player_name, birth_date, nationality | partial dependency, move out -> `player` |
| `(match_id, player_id)` | minutes, position played, goals, assists, xg | full dependency, stays -> `player_match` |

Result: `match`, `player`, `player_match`.

## 3. Third normal form (3NF)

Rule: 2NF, and no non-key column depends on another non-key column (no transitive dependencies).

| Transitive dependency | Fix |
|---|---|
| `match_id -> home_team_id -> home_team_name, home_short` | store only `home_team_id`; names live in `team` |
| `season label` determined by the competition edition | `match.cs_id -> competition_season -> season, competition` |
| `team_id -> short_code, country, founded_year` | `team` table |
| provider ids for a team (`P1-0007`, `P2-0007` ...) | `team_provider_map(provider_id, provider_team_ref) -> team_id` |

The provider map also fixes a many-to-many: one team has one id *per provider*, and the pair `(provider_id, provider_team_ref)`
is the key. `UNIQUE (provider_id, team_id)` states the other direction of the rule.

A team-level fact `(match_id, team_id) -> xg, shots, possession` is not about a single player, so it gets its own table
`match_team`. `player_match` references it with the **composite foreign key** `(match_id, team_id) -> match_team`, which
guarantees a player is only ever attached to a team that actually played in that match, without writing a trigger.

## 4. Boyce-Codd normal form (BCNF)

Rule: for every non-trivial dependency X -> Y, X must be a superkey.

Checking each table's dependencies:

* `match`: besides the primary key `match_id`, the pair `(cs_id, home_team_id, away_team_id)` is also a candidate key (a
  fixture is played once per season). It is declared `UNIQUE`, so both determinants are superkeys. OK.
* `team`: `canonical_name` and `short_code` are both candidate keys (both `UNIQUE`). OK.
* `squad_membership(player_id, team_id, cs_id)`: all-key table. OK.
* `player_rating(player_id, cs_id, head) -> rating`: full key. OK.

No BCNF violations remain in the *core* tables.

## 5. Deliberate denormalisation (what is not in 3NF, and why)

A good design says out loud where it breaks the rules. Four places:

| Redundancy | Why it exists | How it stays correct |
|---|---|---|
| `match.home_goals / away_goals` duplicate `sum(player_match.goals)` | the score is read on every page; summing 22 rows each time is wasteful, and own-goals are not attributed to a scorer in some feeds | integrity check C1 in `sql/04_integrity_checks.sql` must return 0 rows after every load |
| `shot.team_id` and `event.team_id` are determined by `(match_id, player_id)` via `player_match` | `event` has 1.14M rows; dropping a join per query matters more than 4 bytes per row | the generator and the integrity checks use the same rule; a trigger could enforce it in production |
| `match_team.is_home` can be derived from `match.home_team_id` | convenient for `GROUP BY is_home` queries | cheap to verify; check C3 asserts exactly one home row per match |
| `standings` is a whole table of derived numbers | the league table is the single most-requested page; rebuilding it from 380 matches per request is avoidable | maintained inside one ACID transaction (`correct_result()`, Portfolio 3); the view `standings_drift` proves it still equals a fresh recomputation |

The trade-off in one sentence: each denormalisation buys read speed and costs write complexity, so every one comes with a
*check* that detects drift.

## 6. A mistake the checks caught (kept as a lesson)

While building the simulated data, integrity check C2 ("shots with outcome = goal must equal goals in `player_match`")
returned **398 mismatching matches** on the first load. The generator was giving a player goals without giving him
enough shots. The check, not the constraints, found it, because no single `CHECK` can compare two tables. The generator
was fixed (every goal is also a shot) and the check now returns 0 rows.

A second one: while cross-checking the MongoDB leaderboard against PostgreSQL, grouping by `full_name` gave a different
top scorer than grouping by `player_id`, because two different players can share a name. Always group by the key, not
the label.
