-- Load the simulated CSVs produced by data-generator/generate_data.py
-- Run from psql with the folder of CSVs as a variable:
--   psql -d dataxi -v csv=/path/to/csv -f 02_load_data.sql
SET search_path = dataxi, public;
\cd :csv

\copy competition        FROM 'competition.csv'        WITH (FORMAT csv, HEADER)
\copy season             FROM 'season.csv'             WITH (FORMAT csv, HEADER)
\copy competition_season FROM 'competition_season.csv' WITH (FORMAT csv, HEADER)
\copy provider           FROM 'provider.csv'           WITH (FORMAT csv, HEADER)
\copy team               FROM 'team.csv'               WITH (FORMAT csv, HEADER)
\copy team_provider_map  FROM 'team_provider_map.csv'  WITH (FORMAT csv, HEADER)
\copy player             FROM 'player.csv'             WITH (FORMAT csv, HEADER)
\copy squad_membership   FROM 'squad_membership.csv'   WITH (FORMAT csv, HEADER)
\copy match              FROM 'match.csv'              WITH (FORMAT csv, HEADER)
\copy match_team         FROM 'match_team.csv'         WITH (FORMAT csv, HEADER)
\copy player_match       FROM 'player_match.csv'       WITH (FORMAT csv, HEADER)
\copy shot               FROM 'shot.csv'               WITH (FORMAT csv, HEADER)
\copy event              FROM 'event.csv'              WITH (FORMAT csv, HEADER)

VACUUM (ANALYZE);   -- also sets the visibility map, which index-only scans need
SELECT 'match' AS tbl, count(*) FROM match
UNION ALL SELECT 'player_match', count(*) FROM player_match
UNION ALL SELECT 'shot', count(*) FROM shot
UNION ALL SELECT 'event', count(*) FROM event;
