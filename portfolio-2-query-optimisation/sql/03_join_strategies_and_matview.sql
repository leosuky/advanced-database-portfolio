-- Portfolio 2, part B: how the planner chooses a join, and caching a hot query as a materialised view.
-- Run:  psql -d dataxi -f 03_join_strategies_and_matview.sql > ../results/advanced_plans.txt
SET search_path = dataxi, public;
SET max_parallel_workers_per_gather = 0;
\timing off

\echo '=== A. Same join, three algorithms (we disable the planner''s favourite to force the others) ==='
\echo '--- A1. planner default'
EXPLAIN (ANALYZE, COSTS, TIMING)
SELECT count(*) FROM player_match pm JOIN match m ON m.match_id = pm.match_id WHERE m.cs_id = 3;

\echo '--- A2. hash join disabled -> the planner falls back to its second choice'
SET enable_hashjoin = off;
EXPLAIN (ANALYZE, COSTS, TIMING)
SELECT count(*) FROM player_match pm JOIN match m ON m.match_id = pm.match_id WHERE m.cs_id = 3;

\echo '--- A3. hash AND nested loop disabled -> merge join'
SET enable_nestloop = off;
EXPLAIN (ANALYZE, COSTS, TIMING)
SELECT count(*) FROM player_match pm JOIN match m ON m.match_id = pm.match_id WHERE m.cs_id = 3;
RESET enable_hashjoin; RESET enable_nestloop;

\echo '=== B. Cost estimate vs reality (rows estimated vs actual) on a skewed filter ==='
EXPLAIN (ANALYZE, COSTS)
SELECT count(*) FROM event WHERE event_type = 'foul' AND minute > 85;

\echo '=== C. Materialised view for the season scorers leaderboard ==='
DROP MATERIALIZED VIEW IF EXISTS mv_season_scorers;
CREATE MATERIALIZED VIEW mv_season_scorers AS
SELECT m.cs_id, pm.player_id, pm.team_id, sum(pm.goals) AS goals, sum(pm.assists) AS assists, round(sum(pm.xg),2) AS xg
FROM player_match pm JOIN match m ON m.match_id = pm.match_id
GROUP BY m.cs_id, pm.player_id, pm.team_id;
CREATE UNIQUE INDEX ON mv_season_scorers (cs_id, player_id, team_id);   -- required for REFRESH ... CONCURRENTLY
CREATE INDEX ON mv_season_scorers (cs_id, goals DESC);
ANALYZE mv_season_scorers;

\echo '--- C1. Q1 against the base tables'
EXPLAIN (ANALYZE, COSTS OFF)
SELECT pm.player_id, sum(pm.goals) g FROM player_match pm JOIN match m ON m.match_id = pm.match_id AND m.cs_id = 3
GROUP BY pm.player_id ORDER BY g DESC LIMIT 10;

\echo '--- C2. same answer from the materialised view'
EXPLAIN (ANALYZE, COSTS OFF)
SELECT player_id, sum(goals) g FROM mv_season_scorers WHERE cs_id = 3 GROUP BY player_id ORDER BY g DESC LIMIT 10;

\echo '--- C3. cost of keeping it fresh'
\timing on
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_season_scorers;
\timing off

\echo '=== D. Slowest statements seen by pg_stat_statements ==='
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
SELECT calls, round(total_exec_time::numeric,1) AS total_ms, round(mean_exec_time::numeric,2) AS mean_ms, left(regexp_replace(query, '\s+', ' ', 'g'), 90) AS query
FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY total_exec_time DESC LIMIT 5;
