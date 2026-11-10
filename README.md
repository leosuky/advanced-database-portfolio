# MIT 8103 Advanced Database Systems: Practical Portfolio

**Student:** Sunkanmi Samson Otitolaye  |  **Programme:** Master of Information Technology (MIVA Open University), 2026/2027, Semester 1
**Assessment:** First Semester Continuous Assessment, Individual Practical Database Portfolio (40 marks)

---

## Selected case study: The Data XI (football analytics warehouse)

| Item | Statement |
|---|---|
| **Organisation / scenario** | *The Data XI*: a football analytics platform I am building (player ratings, team models, match pages) on top of match data. This portfolio uses a **simulated** version of its database: invented clubs, players and results produced by a seeded generator (`data-generator/generate_data.py`).
| **Nature of the business / system** | An analytics warehouse that stores competitions, seasons, teams, players, matches, player and team match statistics, shots and on-ball events (about 1.14 million rows), and serves league tables, scorer leaderboards, pass networks and match pages. |
| **Users of the database** | (1) the **ingestion pipeline** (writes matches, stats, events); (2) the **public API / website** (read-only, high volume, same questions repeatedly); (3) **analysts / the rating engine** (heavy read queries over all events). They map to the roles `pipeline_writer`, `api_reader` and `analyst` in `portfolio-1/sql/03_roles_and_security.sql`. |
| **Major data stored** | `competition`, `season`, `team`, `player`, `match`, `match_team`, `player_match`, `shot`, `event`, plus provider reconciliation (`provider`, `team_provider_map`), derived `standings` and `player_rating`, and operational `ingestion_job` and `result_audit`. 3 seasons, 20 teams, 1,140 matches, 25,080 player appearances, ~31.7k shots, 1,140,000 events. |
| **Key operations supported** | Load a match from a provider; correct a finished match's score (and keep the league table right); league table; top scorers; team over/under-performance vs xG; rolling form; pass network for a match; live match state and cached match pages; survive a database server failure; spread the biggest table over several servers. |

The same case study and the same data are used in **all five** portfolio activities.

## Where the evidence is

| # | Portfolio activity | Folder | Marks | Main evidence files |
|---|---|---|---|---|
| 1 | Database design and modelling | [`portfolio-1-database-design/`](portfolio-1-database-design) | 7 | `sql/01_schema.sql`, `diagrams/er_diagram.png`, `normalization.md`, `sql/04_integrity_checks.sql` |
| 2 | Query processing and optimisation | [`portfolio-2-query-optimisation/`](portfolio-2-query-optimisation) | 7 | `sql/01_queries.sql`, `results/plans_before/`, `results/plans_after/`, `results/summary.md`, `results/advanced_plans.txt` |
| 3 | Transactions and concurrency | [`portfolio-3-transactions-concurrency/`](portfolio-3-transactions-concurrency) | 7 | `scripts/run_scenarios.py`, `results/scenarios.log` |
| 4 | NoSQL and advanced data models | [`portfolio-4-nosql/`](portfolio-4-nosql) | 7 | `redis/redis_demo.py`, `mongodb/mongo_demo.py`, `results/*.log` |
| 5 | Distributed and cloud database exercise | [`portfolio-5-distributed-cloud/`](portfolio-5-distributed-cloud) | 6 | `scripts/replication_demo.py`, `scripts/sharding_demo.py`, `results/*.log`, `docs/cloud-deployment.md` |
| - | Final documentation | [`docs/`](docs) | 6 | `final-report.docx` / `.pdf`, `ai-use-declaration.md` |

## Headline results (all measured; see each folder for the raw output)

| Area | Result |
|---|---|
| Design | 17 tables in 3NF/BCNF with deliberate, documented denormalisation; 4 cross-table integrity checks return 0 rows; the checks caught 398 inconsistent matches in an early version of the data. |
| Query optimisation | Indexes cut the pass-network query from **77 ms to 0.2 ms (358x)** and a player's passes from **74 ms to 0.6 ms (124x)** (index-only scan). Rewriting a correlated subquery as a window function took **478 ms to 4.6 ms**, beating indexing alone (244 ms). Indexes on the small tables (Q1 to Q4) gave **no measurable benefit**, and that is reported honestly. |
| Transactions | A VAR-style score correction is one atomic transaction (match + two standings rows + audit row). Demonstrated: rollback, savepoints, non-repeatable and phantom reads under READ COMMITTED, snapshot isolation under REPEATABLE READ, a **lost update** (9 wins recorded instead of 10) and two fixes, a **deadlock** and its fix, **write skew** (3 running jobs where the limit is 2) prevented by SERIALIZABLE. |
| NoSQL | Redis: season leaderboard served in **0.52 ms vs 2.31 ms** from PostgreSQL (about 4x through Docker Desktop's VM network; 30x on Linux without that hop), cache-aside with 99% hit rate, and a demonstration of the staleness this causes. MongoDB 7 (real server): match page as one 5.1 KB document, a `$jsonSchema` validator that rejected a bad document (code 121), an index scan examining 19 documents instead of 1,140, and an aggregation pipeline. |
| Distributed / cloud | A real primary and streaming replica: replication lag measured, sync vs async commit cost, a commit that was **acknowledged and then lost** after failover under async replication (and not lost under sync), split-brain explained. Hash sharding of 1.14M events across two nodes: one-shard point queries, scatter-gather for others, and a measured **slowdown** for full scans (52 ms on one node vs about 500 ms sharded; honest finding). Backup verified by restoring it and comparing checksums. Cloud: a deployment **design** only (see below). |

## How to reproduce

Requirements: Docker Desktop and Python 3.10+. Everything runs against the three containers in `docker-compose.yml`.

```bash
docker compose up -d
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

Then follow the "How to run" section of each portfolio README in order (1 to 5). Portfolio 5 runs **inside** the Postgres
container, which already has the PostgreSQL 16 server programs it needs (see `portfolio-5-distributed-cloud/README.md`).
`scripts/run_all.sh` is a convenience script for Portfolios 1 to 4; the submitted results were produced step by step.

The seed is fixed (`8103`), so the data is identical on every run. Timings differ from machine to machine; the *shape* of
the results (which plan is chosen, which anomaly appears) does not.

## Honest notes about how this evidence was produced

* Every result in `results/` was produced by running the code in this repository on my MacBook (Docker Desktop):
  PostgreSQL 16, Redis 7 and MongoDB 7.0.45. The MongoDB logic was first checked with the in-memory `mongomock` library;
  running it on the real server exposed three bugs, which are fixed and listed in `portfolio-4-nosql/README.md`.
* Timings are from one laptop with Docker's virtual network in the path, so absolute numbers are small and network-bound
  in places (noted where it matters, e.g. Redis). Ratios and plan shapes are the evidence, not milliseconds.
* **Cloud deployment was not provisioned for this portfolio.** `docs/cloud-deployment.md` is a design and runbook
  (architecture, self-managed vs managed comparison, security and cost). The database mechanics it depends on
  (base backup, streaming replica, promotion, backup and restore) were run and measured in Portfolio 5.
* Where a result was disappointing (indexes that did nothing, sharding slower than one node) it is reported as measured.

## AI use

See [`docs/ai-use-declaration.md`](docs/ai-use-declaration.md).
