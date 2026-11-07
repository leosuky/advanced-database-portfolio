-- =============================================================================
-- Portfolio 2: the benchmark queries. Each block starts with "-- name: Qn | title"
-- so run_benchmark.py can split the file. Parameters are fixed so results repeat.
-- All run against schema dataxi (search_path is set by the runner).
-- =============================================================================

-- name: Q1 | Top 10 scorers in 2025/26 (join + aggregate)
SELECT p.full_name, t.canonical_name AS team,
       sum(pm.goals) AS goals, sum(pm.assists) AS assists, round(sum(pm.xg),2) AS xg
FROM player_match pm
JOIN match  m ON m.match_id = pm.match_id AND m.cs_id = 3
JOIN player p ON p.player_id = pm.player_id
JOIN team   t ON t.team_id   = pm.team_id
GROUP BY p.player_id, p.full_name, t.canonical_name
ORDER BY goals DESC, xg DESC
LIMIT 10;

-- name: Q2 | League table rebuilt from raw results (UNION ALL + CASE)
WITH sides AS (
    SELECT home_team_id AS team_id, home_goals AS gf, away_goals AS ga FROM match WHERE cs_id = 3 AND status = 'finished'
    UNION ALL
    SELECT away_team_id, away_goals, home_goals FROM match WHERE cs_id = 3 AND status = 'finished'
)
SELECT t.canonical_name, count(*) AS played,
       count(*) FILTER (WHERE gf > ga) AS won,
       count(*) FILTER (WHERE gf = ga) AS drawn,
       count(*) FILTER (WHERE gf < ga) AS lost,
       sum(gf) AS gf, sum(ga) AS ga,
       3 * count(*) FILTER (WHERE gf > ga) + count(*) FILTER (WHERE gf = ga) AS points
FROM sides s JOIN team t USING (team_id)
GROUP BY t.team_id, t.canonical_name
ORDER BY points DESC, sum(gf) - sum(ga) DESC
LIMIT 10;

-- name: Q3 | Which teams over/under-performed their xG in 2025/26?
SELECT t.canonical_name,
       sum(CASE WHEN mt.is_home THEN m.home_goals ELSE m.away_goals END) AS goals,
       round(sum(mt.xg), 1) AS xg,
       round(sum(CASE WHEN mt.is_home THEN m.home_goals ELSE m.away_goals END) - sum(mt.xg), 1) AS goals_minus_xg
FROM match_team mt
JOIN match m ON m.match_id = mt.match_id
JOIN team  t ON t.team_id  = mt.team_id
WHERE m.cs_id = 3
GROUP BY t.team_id, t.canonical_name
ORDER BY goals_minus_xg DESC
LIMIT 5;

-- name: Q4 | Rolling 5-match points form for one team (window function)
WITH results AS (
    SELECT m.kickoff_ts,
           CASE WHEN m.home_team_id = 1 THEN m.home_goals ELSE m.away_goals END AS gf,
           CASE WHEN m.home_team_id = 1 THEN m.away_goals ELSE m.home_goals END AS ga
    FROM match m
    WHERE m.cs_id = 3 AND (m.home_team_id = 1 OR m.away_team_id = 1)
)
SELECT kickoff_ts::date, gf, ga,
       sum(CASE WHEN gf > ga THEN 3 WHEN gf = ga THEN 1 ELSE 0 END)
           OVER (ORDER BY kickoff_ts ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS points_last_5
FROM results
ORDER BY kickoff_ts
LIMIT 10;

-- name: Q5 | Successful passes by one busy player (large-table filter)
SELECT count(*) AS passes, round(avg(end_x - x), 1) AS avg_forward_distance
FROM event
WHERE player_id = 417 AND event_type = 'pass' AND successful;

-- name: Q6 | Pass network for one match (edges between passers and receivers)
SELECT e.player_id AS passer, e.receiver_id AS receiver, count(*) AS passes
FROM event e
WHERE e.match_id = 951 AND e.event_type = 'pass' AND e.successful AND e.receiver_id IS NOT NULL
GROUP BY e.player_id, e.receiver_id
ORDER BY passes DESC
LIMIT 10;

-- name: Q7 | Each team's top scorer, correlated subquery version
SELECT t.canonical_name, p.full_name, s.goals
FROM (
    SELECT pm.team_id, pm.player_id, sum(pm.goals) AS goals
    FROM player_match pm JOIN match m ON m.match_id = pm.match_id AND m.cs_id = 3
    GROUP BY pm.team_id, pm.player_id
) s
JOIN team t   ON t.team_id   = s.team_id
JOIN player p ON p.player_id = s.player_id
WHERE s.goals = (SELECT max(s2.goals) FROM (
                    SELECT pm.player_id, sum(pm.goals) AS goals
                    FROM player_match pm JOIN match m ON m.match_id = pm.match_id AND m.cs_id = 3
                    WHERE pm.team_id = s.team_id GROUP BY pm.player_id) s2)
ORDER BY s.goals DESC
LIMIT 5;

-- name: Q7b | Each team's top scorer, rewritten with a window function
WITH s AS (
    SELECT pm.team_id, pm.player_id, sum(pm.goals) AS goals
    FROM player_match pm JOIN match m ON m.match_id = pm.match_id AND m.cs_id = 3
    GROUP BY pm.team_id, pm.player_id
), ranked AS (
    SELECT s.*, rank() OVER (PARTITION BY team_id ORDER BY goals DESC) AS rk FROM s
)
SELECT t.canonical_name, p.full_name, r.goals
FROM ranked r
JOIN team t   ON t.team_id   = r.team_id
JOIN player p ON p.player_id = r.player_id
WHERE r.rk = 1
ORDER BY r.goals DESC
LIMIT 5;

-- name: Q8 | All goals scored by headers in the 80th minute or later (partial-index candidate)
SELECT count(*) FROM shot WHERE outcome = 'goal' AND body_part = 'head' AND minute >= 80;
