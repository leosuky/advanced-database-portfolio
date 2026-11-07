-- Each statement below SHOULD FAIL. The error text is the evidence that the constraint works.
-- ON_ERROR_STOP is off so psql carries on to the next test.
SET search_path = dataxi, public;
\set VERBOSITY terse

\echo '--- T1: a team cannot play itself (CHECK match_two_teams)'
INSERT INTO match (match_id, cs_id, matchday, kickoff_ts, home_team_id, away_team_id, status)
VALUES (9999, 3, 1, now(), 1, 1, 'scheduled');

\echo '--- T2: a finished match must have a score (CHECK match_score_status)'
BEGIN; UPDATE match SET home_goals = NULL WHERE match_id = 1; ROLLBACK;

\echo '--- T3: a player cannot be attached to a team that did not play the match (composite FK to match_team)'
INSERT INTO player_match (match_id, player_id, team_id, started, minutes_played, position)
SELECT 1, 1, t.team_id, true, 90, 'GK' FROM team t
WHERE t.team_id NOT IN (SELECT team_id FROM match_team WHERE match_id = 1) LIMIT 1;

\echo '--- T4: the same fixture cannot be scheduled twice in a season (UNIQUE cs_id, home, away)'
INSERT INTO match (match_id, cs_id, matchday, kickoff_ts, home_team_id, away_team_id, status)
SELECT 9998, cs_id, 1, now(), home_team_id, away_team_id, 'scheduled' FROM match WHERE match_id = 1;

\echo '--- T5: two teams cannot share a short code (UNIQUE short_code)'
INSERT INTO team (team_id, canonical_name, short_code, country) SELECT 99, 'Test FC', short_code, 'Albion' FROM team WHERE team_id = 1;

\echo '--- T6: a position must be GK/DF/MF/FW (CHECK)'
UPDATE player SET primary_position = 'ST' WHERE player_id = 1;

\echo '--- T7: standings points must equal 3*won + drawn (CHECK standings_points_ok)'
UPDATE standings SET points = points + 1 WHERE cs_id = 3 AND team_id = 1;

\echo '--- T8: deleting a team that has matches is blocked (foreign key)'
DELETE FROM team WHERE team_id = 1;

\echo '--- T9: the api_reader role cannot write (privileges)'
SET ROLE api_reader;
DELETE FROM match WHERE match_id = 1;
RESET ROLE;
