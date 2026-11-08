# Portfolio 5: Distributed and Cloud Database Exercise

Three ideas from the course's Study Session 2 and 3 are applied to The Data XI and **run on real PostgreSQL servers**
(not simulated): replication and consistency, fragmentation (sharding), and backup/recovery and cloud deployment.

| Part | Script | Evidence |
|---|---|---|
| 5a Replication, consistency, availability, failover | `scripts/replication_demo.py` | `results/replication_demo.log` |
| 5b Horizontal fragmentation (sharding) and a federated architecture | `scripts/sharding_demo.py` | `results/sharding_demo.log` |
| 5c Backup and restore | `scripts/backup_restore_demo.sh` | `results/backup_restore.log` |
| 5d Cloud deployment design | `../docs/cloud-deployment.md` | design + student's screenshots |

## How to run

The scripts start their own throw-away PostgreSQL servers (ports 5441 to 5444) with `initdb`, `pg_basebackup` and
`pg_ctl`, so they need the PostgreSQL 16 server programs next to them, plus the `dataxi` database from Portfolio 1. They
refuse to run as root.

**Tested route: inside the Portfolio 1 Postgres container.** The `postgres:16` image already has every PostgreSQL
program at the default `PGBIN` (`/usr/lib/postgresql/16/bin`); only Python is added:

```bash
docker exec -u root ads_postgres bash -c "apt-get update -qq && apt-get install -y -qq python3 python3-psycopg2 bc"
docker exec -u postgres -e RESULTS_DIR=/tmp/p5results ads_postgres bash -c \
  "mkdir -p /tmp/p5results && python3 /work/portfolio-5-distributed-cloud/scripts/replication_demo.py --src-port 5432"
docker cp ads_postgres:/tmp/p5results/replication_demo.log results/
```

(The repo is mounted read-only at `/work`, so logs are written inside the container and copied out. Inside the container
the main server listens on 5432, not 5433. The installed packages disappear with `docker compose down`; `stop`/`start` keep them.)

Alternative on a host with PostgreSQL 16 installed: `export PGBIN=/path/to/postgresql16/bin` and run the same scripts
with `--src-port 5433`.

---

## 5a. Replication, consistency and failover

Topology: `ha_primary :5441` (cloned from `dataxi` with `pg_basebackup`) streaming WAL to `ha_replica :5442`.

| Step | Observation (from `results/replication_demo.log`) | Concept |
|---|---|---|
| 1 | Replica has all 1,140 matches and 1,140,000 events; a write on it fails: `cannot execute INSERT in a read-only transaction`; `pg_stat_replication` shows `state=streaming sync_state=async` | primary-replica, **hot standby**, read scaling |
| 2 | After a 400,000-row write burst the primary had committed in 3.9 s while the replica showed **0** of those rows and was about 2.8 MB (2,833 KiB) of WAL behind; a moment later it showed all 400,000 | **asynchronous replication = eventual consistency**, replication lag |
| 3 | Median commit time for single-row inserts: **0.30 ms asynchronous** vs **0.58 ms synchronous** (`remote_apply`), about twice as slow; with sync, a row acknowledged by the primary was immediately visible on the replica | the price of consistency (here on localhost; a real network round trip is much larger) |
| 4 | With synchronous replication and the replica **down**, the insert was still not acknowledged after 4 s (`pg_stat_activity`: waiting on `IPC/SyncRep`). Cancelling it printed: *"The transaction has already committed locally, but might not have been replicated"*. Switching back to async, the same insert returned in 13 ms | **CAP theorem** in practice: sync chooses consistency (stops accepting writes), async chooses availability |
| 5 | Replica unreachable; primary **acknowledged** a commit; primary crashed; replica promoted. The acknowledged row was **missing** on the new primary | **data-loss window (RPO > 0)** of asynchronous replication |
| 6 | The old primary restarted still believing it was primary, holding a row the new primary never had | **split brain**; needs fencing and a rebuild with `pg_basebackup`/`pg_rewind` |
| 7 | After rebuilding the old primary as a replica and enabling synchronous replication, an acknowledged commit survived the crash of the primary: row present on the promoted standby | synchronous replication gives **RPO = 0** |

Two things found while writing the script that are worth knowing:

* `statement_timeout` does **not** interrupt a commit that is waiting for a synchronous standby. The first version of the
  script hung for more than five minutes. The wait had to be cancelled from another session with `pg_cancel_backend()`.
  In production, sync replication with one standby makes a standby failure an outage unless you list several candidate
  standbys (`synchronous_standby_names = 'ANY 1 (r1, r2)'`).
* `pg_basebackup` waits for a *spread* checkpoint unless you pass `-c fast`; right after a promotion that looked like a hang.

**Consistency models used:** the primary is strongly consistent; an async replica is eventually consistent; Redis in
Portfolio 4 is a cache with TTL-bounded staleness; sync replication is read-your-writes on the replica.

## 5b. Sharding (horizontal fragmentation) with a federated coordinator

```
application -> coordinator DB (:5433, database "coordinator")
                 event_sharded  PARTITION BY HASH (match_id)
                   |-- event_s1 (foreign table) -> shard1 :5443 (separate server)
                   `-- event_s2 (foreign table) -> shard2 :5444 (separate server)
```

*Shard key = `match_id`:* almost every question about events is "for this match", so one match lives entirely on one
shard and per-match queries never cross servers. `postgres_fdw` makes it a **federated** architecture: one SQL
interface over independent nodes.

| Observation (from `results/sharding_demo.log`) | Meaning |
|---|---|
| 1,140,000 events routed by `hash(match_id)`: shard1 617,000 (617 matches), shard2 523,000 (523 matches); coordinator count equals the source | automatic routing; hash gives an 8% imbalance with only 1,140 keys (skew) |
| `WHERE match_id = 951` -> **one** `Foreign Scan` on `event_s2` only | **partition pruning**: only one node is asked |
| `WHERE player_id = 417` -> `Append` over **both** foreign scans | **scatter-gather**: a query without the shard key hits every node |
| Point lookup: 0.12 ms on one node vs 0.55 ms via the coordinator (extra network hop) | sharding adds latency to small queries |
| Full `GROUP BY` over all events: 52 ms on one node vs about 500 ms sharded (roughly 10x slower) | **sharding is not automatically faster.** PostgreSQL 16's `postgres_fdw` pushes filters down but ships all needed rows to the coordinator for aggregation. The gain is capacity, not speed on one machine |
| Join of events with a local `match_local` filter fetched all `match_id`s from both shards (`Remote SQL: SELECT match_id FROM event_shard`) | a filter on a non-shard-key table does not prune shards; look up the match ids first, then query with `match_id IN (...)` |
| With shard2 stopped: matches on shard1 still answered; a query over all matches failed with `could not connect to server "shard2"` | partial availability; add a replica per shard (5a) to remove the gap |
| My formula for the placement rule reproduced PostgreSQL's real placement (617/523) and says going from 2 to 3 shards would move **739 of 1,140 matches (65%)** | resharding by `hash mod N` is expensive; consistent hashing or range/directory sharding moves less |

**Costs of this design (the reasons not to shard early):** no global primary key or foreign keys across shards
(`event_id` is only unique within a shard); no cross-shard transactions without two-phase commit; operational complexity.
At 1.14M rows one PostgreSQL node is the right answer; the exercise shows what changes if the event table were 1,000
times larger.

## 5c. Backup and restore (measured)

| Step | Result (`results/backup_restore.log`) |
|---|---|
| Source | 197 MB database (1,140 matches, 25,080 appearances, 1,140,000 events) |
| `pg_dump -Fc` | 3.5 s, 18 MB |
| `pg_restore -j 4` into a new database | 4.3 s |
| Verification | row counts equal for four tables, and an md5 checksum over all 1.14M event rows is **identical** |

A logical nightly dump means up to 24 hours of lost writes (RPO = 24 h). Point-in-time recovery needs WAL archiving;
replication (5a) is **not** a backup, since a bad `DELETE` replicates instantly.

## 5d. Cloud deployment

See [`../docs/cloud-deployment.md`](../docs/cloud-deployment.md): architecture across two availability domains,
self-managed VM vs managed service comparison, runbook, security checklist and the evidence checklist the student fills in
on the real cloud VM.

## Limitations

* All nodes run on one machine, so network latency, bandwidth and independent hardware failure are not real. The
  *behaviour* (what is acknowledged, lost, blocked or pruned) is real; absolute times are not representative.
* A production failover would be orchestrated (Patroni, a managed service) rather than a script.
