-- Portfolio 3 set-up: build the denormalised standings table and the transactional
-- result-correction procedure. Safe to re-run (it rebuilds the standings from `match`).
SET search_path = dataxi, public;

CREATE OR REPLACE FUNCTION rebuild_standings(p_cs smallint) RETURNS void
LANGUAGE sql AS $$
    DELETE FROM standings WHERE cs_id = p_cs;
    INSERT INTO standings (cs_id, team_id, played, won, drawn, lost, goals_for, goals_against, points)
    WITH sides AS (
        SELECT home_team_id AS team_id, home_goals AS gf, away_goals AS ga FROM match WHERE cs_id = p_cs AND status = 'finished'
        UNION ALL
        SELECT away_team_id, away_goals, home_goals FROM match WHERE cs_id = p_cs AND status = 'finished')
    SELECT p_cs, team_id, count(*),
           count(*) FILTER (WHERE gf > ga), count(*) FILTER (WHERE gf = ga), count(*) FILTER (WHERE gf < ga),
           sum(gf), sum(ga), 3 * count(*) FILTER (WHERE gf > ga) + count(*) FILTER (WHERE gf = ga)
    FROM sides GROUP BY team_id;
$$;

-- Add (sign = +1) or remove (sign = -1) one match result from both teams' standings rows.
CREATE OR REPLACE FUNCTION apply_result(p_cs smallint, p_home int, p_away int, hg int, ag int, sign int) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE standings SET
        played        = played + sign,
        won           = won    + sign * (hg > ag)::int,
        drawn         = drawn  + sign * (hg = ag)::int,
        lost          = lost   + sign * (hg < ag)::int,
        goals_for     = goals_for     + sign * hg,
        goals_against = goals_against + sign * ag,
        points        = points + sign * (CASE WHEN hg > ag THEN 3 WHEN hg = ag THEN 1 ELSE 0 END)
    WHERE cs_id = p_cs AND team_id = p_home;
    UPDATE standings SET
        played        = played + sign,
        won           = won    + sign * (ag > hg)::int,
        drawn         = drawn  + sign * (hg = ag)::int,
        lost          = lost   + sign * (ag < hg)::int,
        goals_for     = goals_for     + sign * ag,
        goals_against = goals_against + sign * hg,
        points        = points + sign * (CASE WHEN ag > hg THEN 3 WHEN hg = ag THEN 1 ELSE 0 END)
    WHERE cs_id = p_cs AND team_id = p_away;
END $$;

-- One ACID unit of work: a VAR-style correction of a finished match's score.
-- Either the match row, both standings rows AND the audit row change, or nothing does.
CREATE OR REPLACE FUNCTION correct_result(p_match int, p_hg int, p_ag int, p_reason text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE m match%ROWTYPE;
BEGIN
    SELECT * INTO m FROM match WHERE match_id = p_match FOR UPDATE;       -- lock the match row
    IF NOT FOUND THEN RAISE EXCEPTION 'match % does not exist', p_match; END IF;
    IF m.status <> 'finished' THEN RAISE EXCEPTION 'match % is %, only finished matches can be corrected', p_match, m.status; END IF;

    -- Lock both standings rows in a FIXED order (lowest team_id first). Two corrections
    -- touching the same teams then queue up instead of deadlocking (see scenario 5).
    PERFORM 1 FROM standings WHERE cs_id = m.cs_id AND team_id IN (m.home_team_id, m.away_team_id) ORDER BY team_id FOR UPDATE;

    PERFORM apply_result(m.cs_id, m.home_team_id, m.away_team_id, m.home_goals, m.away_goals, -1);   -- undo old
    PERFORM apply_result(m.cs_id, m.home_team_id, m.away_team_id, p_hg, p_ag, +1);                   -- apply new
    UPDATE match SET home_goals = p_hg, away_goals = p_ag WHERE match_id = p_match;
    INSERT INTO result_audit (match_id, old_home_goals, old_away_goals, new_home_goals, new_away_goals, reason)
    VALUES (p_match, m.home_goals, m.away_goals, p_hg, p_ag, p_reason);
END $$;

-- Cross-check used by the scripts: standings must equal a fresh recomputation from `match`.
CREATE OR REPLACE VIEW standings_drift AS
WITH sides AS (
    SELECT cs_id, home_team_id AS team_id, home_goals AS gf, away_goals AS ga FROM match WHERE status = 'finished'
    UNION ALL SELECT cs_id, away_team_id, away_goals, home_goals FROM match WHERE status = 'finished'),
fresh AS (
    SELECT cs_id, team_id, count(*) AS played, sum(gf) AS gf, sum(ga) AS ga,
           3 * count(*) FILTER (WHERE gf > ga) + count(*) FILTER (WHERE gf = ga) AS pts
    FROM sides GROUP BY cs_id, team_id)
SELECT s.cs_id, s.team_id, s.points AS stored_points, f.pts AS fresh_points
FROM standings s JOIN fresh f USING (cs_id, team_id)
WHERE s.points <> f.pts OR s.played <> f.played OR s.goals_for <> f.gf OR s.goals_against <> f.ga;

SELECT rebuild_standings(1::smallint), rebuild_standings(2::smallint), rebuild_standings(3::smallint);
