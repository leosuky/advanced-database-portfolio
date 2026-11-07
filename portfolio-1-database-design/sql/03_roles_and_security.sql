-- Three kinds of database user (see README "Users of the database").
-- Least privilege: the API can only read, analysts can read everything,
-- the pipeline can write facts but cannot touch reference data.
SET search_path = dataxi, public;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'api_reader')      THEN CREATE ROLE api_reader      NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analyst')         THEN CREATE ROLE analyst         NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pipeline_writer') THEN CREATE ROLE pipeline_writer NOLOGIN; END IF;
END $$;

GRANT USAGE ON SCHEMA dataxi TO api_reader, analyst, pipeline_writer;

-- API: read the public-facing tables only (no ingestion/audit internals)
GRANT SELECT ON competition, season, competition_season, team, player, squad_membership,
                match, match_team, player_match, shot, event, standings TO api_reader;

-- Analysts: read everything
GRANT SELECT ON ALL TABLES IN SCHEMA dataxi TO analyst;

-- Pipeline: insert/update the fact + operational tables, read the dimensions
GRANT SELECT ON ALL TABLES IN SCHEMA dataxi TO pipeline_writer;
GRANT INSERT, UPDATE ON match, match_team, player_match, shot, event, standings,
                         ingestion_job, result_audit, player_rating TO pipeline_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA dataxi TO pipeline_writer;
