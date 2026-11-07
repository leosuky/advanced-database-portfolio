-- Indexes added after measuring the "before" plans.
-- Reason for each one is in portfolio-2/README.md.
SET search_path = dataxi, public;

-- Postgres does NOT create indexes for foreign keys automatically.
CREATE INDEX idx_event_match          ON event (match_id);                       -- Q6
CREATE INDEX idx_event_player_type    ON event (player_id, event_type)           -- Q5 (covering, index-only scan)
       INCLUDE (successful, x, end_x);
CREATE INDEX idx_match_cs             ON match (cs_id);                          -- Q1-Q4, Q7
CREATE INDEX idx_match_home           ON match (cs_id, home_team_id);            -- Q4
CREATE INDEX idx_match_away           ON match (cs_id, away_team_id);            -- Q4
CREATE INDEX idx_player_match_team    ON player_match (team_id, player_id);      -- Q7 correlated lookup
CREATE INDEX idx_player_match_player  ON player_match (player_id);               -- FK lookup
-- Partial index: only the rare rows we search for (goals), so it stays tiny.
CREATE INDEX idx_shot_goals_late      ON shot (body_part, minute) WHERE outcome = 'goal';  -- Q8

ANALYZE;
