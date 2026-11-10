# Cloud deployment plan (Portfolio 5, cloud part)

> **Status: design only.** This is a deployment design and runbook; no cloud resources were provisioned for this
> portfolio. The database mechanics it relies on (base backup, streaming replica, promotion, backup and restore) were run
> and measured locally in Docker (`portfolio-5-distributed-cloud/results/`). The steps below are how the design would be deployed.

## 1. Target architecture

```
                         Internet
                             |
                  [ Load balancer / reverse proxy: HTTPS only ]
                             |
                    +-----------------+
                    |  API container  |  (read-only role: api_reader)
                    +--------+--------+
                             |  private subnet only
              +--------------+---------------+
              |                              |
   +----------v----------+        +----------v----------+
   |  PostgreSQL PRIMARY |------->|  PostgreSQL REPLICA |   streaming replication
   |  AD-1 (writes)      |  WAL   |  AD-2 (reads, DR)   |   (async by default, sync for corrections)
   +----------+----------+        +---------------------+
              |
   nightly pg_dump + WAL archive  --->  Object Storage bucket (different region / tenancy)
   Redis cache (same private subnet)
```

* **Two availability domains / zones** so one data-centre failure does not take out both copies.
* **Private subnet** for the databases: only the API and an admin bastion can reach port 5432.
* **Roles** from Portfolio 1: the API connects as `api_reader`, the pipeline as `pipeline_writer`, analysts as `analyst`.

## 2. Self-managed VM vs managed database service

| | Self-managed on a VM (Oracle Cloud Ampere VM, Docker) | Managed service (OCI Database with PostgreSQL, AWS RDS, Google Cloud SQL) |
|---|---|---|
| Cost | lowest: one small VM (the project's server budget is under $20 a month) | higher; managed HA, backups, patching |
| Setup effort | configure replication, backups, failover, monitoring | a few clicks; replica, automated backups and failover included |
| Failover | manual or scripted (`pg_ctl promote` after fencing) | automatic, usually a minute or two |
| Control | full (extensions such as `postgres_fdw`, custom settings) | restricted superuser, a fixed list of extensions |
| Data protection (NDPA 2023) | you control the region and who has the disk | choose a region deliberately; cross-border transfer rules apply |
| Sensible when | learning, small budget, a single developer | production with paying users and an uptime promise |

**Recommendation for The Data XI today:** self-managed on the existing VM (cost, and the learning value), with
a **separate** nightly backup to object storage. Move to a managed service if the project ever carries an uptime promise
or the operator's time is worth more than the monthly fee.

## 3. Runbook (how the design would be deployed on the VM)

1. **Provision.** Ubuntu 22.04/24.04 on an Ampere A1 (ARM) VM; open only ports 22 (your IP) and 443 in the security list.
2. **Install.** `sudo apt install postgresql-16 postgresql-client-16` (or the `postgres:16` Docker image: ARM images exist).
3. **Primary settings** (`postgresql.conf`): `wal_level = replica`, `max_wal_senders = 10`, `listen_addresses = '<private ip>'`,
   `password_encryption = scram-sha-256`. In `pg_hba.conf` allow `replication` and the app only from the private subnet,
   method `scram-sha-256`. **Never use `trust`** as the local demos do.
4. **Replica** (tested locally, see `scripts/replication_demo.py`):
   `pg_basebackup -h <primary> -U replicator -D /var/lib/postgresql/16/main -X stream -R -c fast`
5. **Synchronous mode for corrections only:** `synchronous_commit = remote_apply` set per transaction
   (`SET LOCAL synchronous_commit = 'remote_apply'`) inside `correct_result()`'s caller, and the default left at `local` for bulk ingestion.
6. **Backups** (tested locally, see `results/backup_restore.log`): nightly `pg_dump -Fc`, copy to Object Storage with a
   retention rule; **test a restore monthly**. A backup that has never been restored is a hope, not a backup.
7. **Monitoring:** `pg_stat_replication` lag, disk space, `pg_stat_statements`, and an alert if the replica is more than
   a few MB of WAL behind.
8. **Failover drill:** stop the primary, fence it (stop the VM from the cloud console so it cannot return as a second
   primary), `pg_ctl promote` on the replica, repoint the API, rebuild the old primary as a replica
   (`pg_basebackup`). Practise it before you need it.

## 4. Security checklist

| Control | Why |
|---|---|
| TLS between API and database (`ssl = on`) | credentials and data in transit |
| `scram-sha-256`, no `trust` | password protection |
| Least-privilege roles (Portfolio 1) | the public API cannot write or read internals |
| Private subnet, security list allows 5432 only from the API | database is not on the internet |
| Encrypted volumes and backups | data at rest |
| No real personal data in non-production copies | the simulated dataset is synthetic by design (NDPA 2023) |

## 5. Estimated cost (indicative; check current provider pricing)

One always-on ARM VM within the provider's budget, plus a small object-storage bucket for backups, fits the
"under $20 a month" target. A managed PostgreSQL primary plus replica on any of the big three clouds is typically a
multiple of that. Verify on the provider's pricing page before deciding; prices change.

## 6. Evidence a real deployment would need to show (not captured for this portfolio)

- [ ] VM details page in the cloud console (shape, region, availability domain)
- [ ] `psql -c "select version()"` on the VM, and `pg_stat_replication` showing the replica `streaming`
- [ ] The security list / firewall showing 5432 is **not** open to the internet
- [ ] Object Storage bucket listing with the nightly dump
- [ ] The output of `replication_demo.py` run **on the VM** (or a screenshot of the failover drill)
