# Portfolio 2: Query Processing and Optimisation

Same database as Portfolio 1 (PostgreSQL 16, 1.14M `event` rows). Every number below is copied from the files in
`results/`, which `run_benchmark.py` generates.

## Method

1. Start with **only the primary-key and UNIQUE indexes** (PostgreSQL does *not* index foreign keys automatically).
2. For each of nine queries (`sql/01_queries.sql`) run `EXPLAIN (ANALYZE, BUFFERS)` 15 times, throw away the first
   (cold cache) and record the **median** of the other 14 (`results/plans_before/`).
3. Create the indexes in `sql/02_indexes.sql`, run `ANALYZE`, repeat (`results/plans_after/`).
4. Parallel workers are switched off so the before and after runs are comparable.

```bash
python run_benchmark.py --dsn "host=127.0.0.1 port=5433 dbname=dataxi user=postgres"
psql -d dataxi -f sql/03_join_strategies_and_matview.sql > results/advanced_plans.txt
```

## Results

| Query | What it does | Before (ms) | After (ms) | Speed-up | Plan change |
|---|---|---:|---:|---:|---|
| Q1 | Top 10 scorers of a season (3-table join + aggregate) | 7.0 | 6.9 | 1.0x | no meaningful change |
| Q2 | League table rebuilt from results (UNION ALL + FILTER) | 0.5 | 0.4 | 1.1x | no meaningful change |
| Q3 | Goals vs xG per team | 0.7 | 0.6 | 1.1x | no meaningful change |
| Q4 | Rolling 5-match form (window function) | 0.1 | 0.1 | 1.1x | no meaningful change |
| **Q5** | One player's successful passes | 73.8 | **0.6** | **124x** | Seq Scan -> **Index Only Scan** |
| **Q6** | Pass network for one match | 77.2 | **0.2** | **358x** | Seq Scan -> **Index Scan** |
| Q7 | Top scorer per team, correlated subquery | 477.8 | 243.7 | 2.0x | index helps, but the query shape is the problem |
| **Q7b** | Same answer, window function (`rank() OVER`) | 4.6 | 4.3 | 1.1x | **about 100x faster than Q7 even before indexing** |
| **Q8** | Late header goals (partial index) | 1.6 | **0.02** | ~100x | Seq Scan -> Index Only Scan, 0 heap fetches |

Index sizes (what the speed costs in disk): `idx_event_player_type` 48 MB, `idx_event_match` 8 MB, everything else under
200 kB.

## Reading the plans (what each result shows)

### Q6: the foreign-key index nobody created

Before (`plans_before/Q6.txt`):

```
Seq Scan on event e  (actual time=64.6..77.7 rows=476)
  Filter: (successful AND receiver_id IS NOT NULL AND match_id = 951 AND event_type = 'pass')
  Rows Removed by Filter: 1139524
  Buffers: shared hit=12960
```

PostgreSQL read **12,960 pages** to return **476 rows**: it examined all 1,140,000 events to keep 0.04% of them. After
`CREATE INDEX idx_event_match ON event (match_id)`:

```
Index Scan using idx_event_match on event e  (actual time=0.003..0.119 rows=476)
  Index Cond: (match_id = 951)       Buffers: shared hit=16
```

16 buffers instead of 12,960. The planner's *estimated* cost fell from about 30,000 to 39 (`cost=` figures).

### Q5: a covering index and an index-only scan

`CREATE INDEX idx_event_player_type ON event (player_id, event_type) INCLUDE (successful, x, end_x)` contains every
column the query needs, so PostgreSQL never visits the table (`Heap Fetches: 0`). That needed a `VACUUM` first: index-only
scans rely on the visibility map. Without it the plan became a Bitmap Heap Scan (seen on an earlier run, before
`02_load_data.sql` was changed to `VACUUM (ANALYZE)`).

### Q8: partial index

`WHERE outcome = 'goal'` indexes only the 2,777 goals out of 31,659 shots, so the index is **48 kB, against 240 kB for the
same index without the `WHERE`** (measured), and the query becomes an index-only scan in about 0.02 ms.

### Q7 vs Q7b: the biggest win is not an index

The correlated subquery re-aggregates the team's whole season **for every candidate row** (`SubPlan 1` in the plan):
**109,508 buffer hits** in `plans_before/Q7.txt`. Indexes cut the time (478 to 244 ms) but cannot change the algorithm.
Rewriting it as `rank() OVER (PARTITION BY team_id ORDER BY goals DESC)` computes the aggregate once: **311 buffer hits**
and 4.6 ms with **no** new index (`plans_before/Q7b.txt`).

### Q1 to Q4: indexes that did not help (honest result)

`match` has 1,140 rows and `player_match` 25,080. Reading them completely is cheaper than walking an index, so the
planner keeps using sequential scans, and timing differences are of a few tenths of a millisecond. **`idx_match_cs`, `idx_match_home` and
`idx_match_away` are therefore not justified by this data size.** They are kept only because the tables grow every
season; in a real review I would drop them until `match` has tens of thousands of rows. Indexes cost write time and
disk, so adding an index is not always a drastic improvement.

## Join algorithms and cost estimates (`results/advanced_plans.txt`)

The same join (`player_match JOIN match WHERE cs_id = 3`, 8,360 result rows) forced into three algorithms:

| Algorithm | How it works | Estimated cost | Measured |
|---|---|---:|---:|
| Hash Join (planner's choice, A1) | build a hash table of the 380 matches, stream the 25,080 appearances through it | 570 | 4.5 ms |
| Nested Loop (hash disabled, A2) | for each of 380 matches, probe the primary-key index of `player_match` | 658 | 2.4 ms |
| Merge Join (hash and nested loop disabled, A3) | read both inputs in key order and merge them | 860 | 4.2 ms |

Observation: the planner's cost model ranked Hash Join cheapest, but in this run the Nested Loop was about twice as
fast. Cost figures are estimates (they do not know that every page is already in memory), so measure with `EXPLAIN
ANALYZE` rather than trust the estimate. Each figure is a single `EXPLAIN ANALYZE` run, so the 2x gap on a 4 ms query
is indicative, not proof. A second example in the same file: `count(*) FROM event WHERE event_type='foul' AND
minute > 85` is estimated at 3,245 rows and actually returns 3,209 (good statistics after `ANALYZE`).

## Materialised view

`mv_season_scorers` pre-computes the leaderboard. Reading the top 10 from it takes **0.3 ms against 3.8 ms** for the
same answer from the base tables, and `REFRESH MATERIALIZED VIEW CONCURRENTLY` takes about 22 ms (it needs the unique
index on the view). The price is **staleness**: the view is out of date until refreshed. That is acceptable for a
leaderboard refreshed after each match day, and unacceptable for the standings during a correction (which is why
`standings` is maintained transactionally in Portfolio 3).

## `pg_stat_statements`

Section D of `results/advanced_plans.txt` lists the heaviest statements the server has seen. The `COPY event` bulk load (16.1 s,
once) and the benchmark's own `EXPLAIN ANALYZE` calls dominate, which confirms that the `event` table is where
performance work matters.

## Limitations

* Single machine, warm cache, 14 repetitions: absolute numbers will differ on other hardware but ratios and plan shapes
  should not.
* Sub-millisecond queries are dominated by noise.
* A production system would also look at `work_mem`, parallel query and autovacuum settings; these are left at defaults
  here, with parallel query disabled for fair comparison.
