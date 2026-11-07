-- =============================================================================
-- MIT 8103 Advanced Database Systems - Portfolio 1
-- Case study: The Data XI (football analytics warehouse)
-- PostgreSQL 16. Schema: dataxi
--
-- Run:  psql -d dataxi -f 01_schema.sql
-- =============================================================================

DROP SCHEMA IF EXISTS dataxi CASCADE;
CREATE SCHEMA dataxi;
SET search_path = dataxi, public;

-- -----------------------------------------------------------------------------
-- Reference / dimension tables
-- -----------------------------------------------------------------------------

CREATE TABLE competition (
    competition_id   smallint    PRIMARY KEY,
    name             text        NOT NULL UNIQUE,
    country          text        NOT NULL,
    tier             smallint    NOT NULL CHECK (tier BETWEEN 1 AND 5)
);

CREATE TABLE season (
    season_id        smallint    PRIMARY KEY,
    label            text        NOT NULL UNIQUE,           -- e.g. '2025/26'
    start_date       date        NOT NULL,
    end_date         date        NOT NULL,
    CONSTRAINT season_dates_ok CHECK (end_date > start_date)
);

-- One row per competition in one season (the "competition edition").
CREATE TABLE competition_season (
    cs_id            smallint    PRIMARY KEY,
    competition_id   smallint    NOT NULL REFERENCES competition,
    season_id        smallint    NOT NULL REFERENCES season,
    UNIQUE (competition_id, season_id)
);

CREATE TABLE team (
    team_id          integer     PRIMARY KEY,
    canonical_name   text        NOT NULL UNIQUE,
    short_code       char(3)     NOT NULL UNIQUE,           -- must be unique (see Team Registry note)
    country          text        NOT NULL,
    founded_year     smallint    CHECK (founded_year BETWEEN 1850 AND 2100)
);

CREATE TABLE player (
    player_id        integer     PRIMARY KEY,
    full_name        text        NOT NULL,
    birth_date       date        NOT NULL,
    nationality      text        NOT NULL,
    primary_position char(2)     NOT NULL CHECK (primary_position IN ('GK','DF','MF','FW'))
);

-- A player belongs to a team for part or all of a season.
CREATE TABLE squad_membership (
    player_id        integer     NOT NULL REFERENCES player,
    team_id          integer     NOT NULL REFERENCES team,
    cs_id            smallint    NOT NULL REFERENCES competition_season,
    shirt_number     smallint    CHECK (shirt_number BETWEEN 1 AND 99),
    PRIMARY KEY (player_id, team_id, cs_id)
);

-- -----------------------------------------------------------------------------
-- Provider reconciliation (the real platform merges four data providers)
-- Providers are anonymised here: provider_a .. provider_d.
-- -----------------------------------------------------------------------------

CREATE TABLE provider (
    provider_id      smallint    PRIMARY KEY,
    name             text        NOT NULL UNIQUE,
    role             text        NOT NULL CHECK (role IN ('anchor','supplementary'))
);

CREATE TABLE team_provider_map (
    provider_id      smallint    NOT NULL REFERENCES provider,
    provider_team_ref text       NOT NULL,
    team_id          integer     NOT NULL REFERENCES team,
    PRIMARY KEY (provider_id, provider_team_ref),
    UNIQUE (provider_id, team_id)                           -- one external id per team per provider
);

-- -----------------------------------------------------------------------------
-- Facts
-- -----------------------------------------------------------------------------

CREATE TABLE match (
    match_id         integer     PRIMARY KEY,
    cs_id            smallint    NOT NULL REFERENCES competition_season,
    matchday         smallint    NOT NULL CHECK (matchday BETWEEN 1 AND 60),
    kickoff_ts       timestamptz NOT NULL,
    home_team_id     integer     NOT NULL REFERENCES team,
    away_team_id     integer     NOT NULL REFERENCES team,
    home_goals       smallint    CHECK (home_goals >= 0),
    away_goals       smallint    CHECK (away_goals >= 0),
    status           text        NOT NULL DEFAULT 'scheduled'
                                 CHECK (status IN ('scheduled','live','finished','postponed')),
    CONSTRAINT match_two_teams    CHECK (home_team_id <> away_team_id),
    -- a finished match must have a score, an unfinished one must not
    CONSTRAINT match_score_status CHECK (
        (status = 'finished' AND home_goals IS NOT NULL AND away_goals IS NOT NULL)
     OR (status <> 'finished' AND home_goals IS NULL AND away_goals IS NULL)),
    UNIQUE (cs_id, home_team_id, away_team_id)               -- one home fixture per pairing per season
);

-- Team-level numbers for one side of one match.
CREATE TABLE match_team (
    match_id         integer     NOT NULL REFERENCES match,
    team_id          integer     NOT NULL REFERENCES team,
    is_home          boolean     NOT NULL,
    xg               numeric(5,2) NOT NULL CHECK (xg >= 0),
    shots            smallint    NOT NULL CHECK (shots >= 0),
    possession_pct   numeric(4,1) CHECK (possession_pct BETWEEN 0 AND 100),
    PRIMARY KEY (match_id, team_id)
);

-- Player-level numbers for one player in one match.
-- (match_id, team_id) must be a team that actually played in that match:
-- the composite FK to match_team enforces it without a trigger.
CREATE TABLE player_match (
    match_id         integer     NOT NULL,
    player_id        integer     NOT NULL REFERENCES player,
    team_id          integer     NOT NULL,
    started          boolean     NOT NULL,
    minutes_played   smallint    NOT NULL CHECK (minutes_played BETWEEN 0 AND 130),
    position         char(2)     NOT NULL CHECK (position IN ('GK','DF','MF','FW')),
    goals            smallint    NOT NULL DEFAULT 0 CHECK (goals >= 0),
    assists          smallint    NOT NULL DEFAULT 0 CHECK (assists >= 0),
    shots            smallint    NOT NULL DEFAULT 0 CHECK (shots >= 0),
    xg               numeric(5,2) NOT NULL DEFAULT 0 CHECK (xg >= 0),
    passes           smallint    NOT NULL DEFAULT 0 CHECK (passes >= 0),
    tackles          smallint    NOT NULL DEFAULT 0 CHECK (tackles >= 0),
    PRIMARY KEY (match_id, player_id),
    FOREIGN KEY (match_id, team_id) REFERENCES match_team (match_id, team_id)
);

CREATE TABLE shot (
    shot_id          integer     PRIMARY KEY,
    match_id         integer     NOT NULL REFERENCES match,
    player_id        integer     NOT NULL REFERENCES player,
    team_id          integer     NOT NULL REFERENCES team,
    minute           smallint    NOT NULL CHECK (minute BETWEEN 0 AND 130),
    x                numeric(4,1) NOT NULL CHECK (x BETWEEN 0 AND 100),
    y                numeric(4,1) NOT NULL CHECK (y BETWEEN 0 AND 100),
    xg               numeric(4,3) NOT NULL CHECK (xg BETWEEN 0 AND 1),
    outcome          text        NOT NULL CHECK (outcome IN ('goal','saved','blocked','off_target','post')),
    body_part        text        NOT NULL CHECK (body_part IN ('right_foot','left_foot','head','other'))
);

-- The big table: every on-ball event (passes, tackles, carries...). ~1M+ rows.
CREATE TABLE event (
    event_id         bigint      PRIMARY KEY,
    match_id         integer     NOT NULL REFERENCES match,
    team_id          integer     NOT NULL REFERENCES team,
    player_id        integer     NOT NULL REFERENCES player,
    minute           smallint    NOT NULL CHECK (minute BETWEEN 0 AND 130),
    second           smallint    NOT NULL CHECK (second BETWEEN 0 AND 59),
    event_type       text        NOT NULL
                                 CHECK (event_type IN ('pass','carry','tackle','interception','clearance','duel','foul')),
    x                numeric(4,1) NOT NULL,
    y                numeric(4,1) NOT NULL,
    end_x            numeric(4,1),
    end_y            numeric(4,1),
    successful       boolean     NOT NULL,
    receiver_id      integer     REFERENCES player            -- only set for passes
);

-- -----------------------------------------------------------------------------
-- Derived / operational tables
-- -----------------------------------------------------------------------------

-- Denormalised league table, maintained by the result-processing transaction
-- (Portfolio 3). It can always be rebuilt from `match` (Portfolio 2, query Q2).
CREATE TABLE standings (
    cs_id            smallint    NOT NULL REFERENCES competition_season,
    team_id          integer     NOT NULL REFERENCES team,
    played           smallint    NOT NULL DEFAULT 0 CHECK (played >= 0),
    won              smallint    NOT NULL DEFAULT 0 CHECK (won >= 0),
    drawn            smallint    NOT NULL DEFAULT 0 CHECK (drawn >= 0),
    lost             smallint    NOT NULL DEFAULT 0 CHECK (lost >= 0),
    goals_for        smallint    NOT NULL DEFAULT 0 CHECK (goals_for >= 0),
    goals_against    smallint    NOT NULL DEFAULT 0 CHECK (goals_against >= 0),
    points           smallint    NOT NULL DEFAULT 0 CHECK (points >= 0),
    PRIMARY KEY (cs_id, team_id),
    CONSTRAINT standings_played_sums CHECK (played = won + drawn + lost),
    CONSTRAINT standings_points_ok   CHECK (points = 3 * won + drawn)
);

CREATE TABLE player_rating (
    player_id        integer     NOT NULL REFERENCES player,
    cs_id            smallint    NOT NULL REFERENCES competition_season,
    head             text        NOT NULL
                                 CHECK (head IN ('goalkeeper','ball_winner','creation','progressive_threat','finishing')),
    rating           numeric(5,2) NOT NULL,
    computed_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, cs_id, head)
);

CREATE TABLE ingestion_job (
    job_id           bigserial   PRIMARY KEY,
    provider_id      smallint    NOT NULL REFERENCES provider,
    match_id         integer     NOT NULL REFERENCES match,
    status           text        NOT NULL DEFAULT 'running'
                                 CHECK (status IN ('running','succeeded','failed')),
    rows_loaded      integer     NOT NULL DEFAULT 0 CHECK (rows_loaded >= 0),
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz
);

CREATE TABLE result_audit (
    audit_id         bigserial   PRIMARY KEY,
    match_id         integer     NOT NULL REFERENCES match,
    old_home_goals   smallint,
    old_away_goals   smallint,
    new_home_goals   smallint,
    new_away_goals   smallint,
    changed_at       timestamptz NOT NULL DEFAULT now(),
    reason           text
);
