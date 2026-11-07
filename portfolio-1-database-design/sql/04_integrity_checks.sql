-- Cross-table consistency checks that no single constraint can express.
-- Every query should return zero rows. Used after each load.
SET search_path = dataxi, public;

-- C1: a finished match's score equals the sum of player goals per team
SELECT m.match_id
FROM match m
JOIN LATERAL (SELECT coalesce(sum(goals),0) g FROM player_match pm WHERE pm.match_id=m.match_id AND pm.team_id=m.home_team_id) h ON true
JOIN LATERAL (SELECT coalesce(sum(goals),0) g FROM player_match pm WHERE pm.match_id=m.match_id AND pm.team_id=m.away_team_id) a ON true
WHERE m.status='finished' AND (m.home_goals<>h.g OR m.away_goals<>a.g);

-- C2: shots with outcome 'goal' equal goals in player_match, per match
SELECT s.match_id FROM (SELECT match_id, count(*) FILTER (WHERE outcome='goal') n FROM shot GROUP BY match_id) s
JOIN (SELECT match_id, sum(goals) n FROM player_match GROUP BY match_id) p USING (match_id)
WHERE s.n <> p.n;

-- C3: every match has exactly one home and one away match_team row
SELECT match_id FROM match_team GROUP BY match_id
HAVING count(*) <> 2 OR count(*) FILTER (WHERE is_home) <> 1;

-- C4: a pass receiver is never the passer
SELECT event_id FROM event WHERE receiver_id = player_id;
