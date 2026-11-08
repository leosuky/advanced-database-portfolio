#!/usr/bin/env python3
"""
Portfolio 5b - horizontal fragmentation (sharding) of the `event` table across two PostgreSQL nodes.

    coordinator (database `coordinator` on the main cluster, :5433)   <- the application connects here
        event_sharded  PARTITION BY HASH (match_id)
            |-- event_s1  FOREIGN TABLE -> shard1 :5443
            `-- event_s2  FOREIGN TABLE -> shard2 :5444

Shard key = match_id, because almost every question about events is "for this match ...".
All events of one match live on ONE shard, so a match page needs only one node.

Uses postgres_fdw (federated architecture, MIVA study session 2). Run as a normal user:
    python sharding_demo.py --src-port 5433
Set PGBIN as for replication_demo.py.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
PGBIN = os.environ.get("PGBIN", "/usr/lib/postgresql/16/bin")
BASE = os.environ.get("SHARD_BASE", "/tmp/ads_shards")
S1, S2 = 5443, 5444
lines = []


def say(m=""):
    print(m, flush=True)
    lines.append(m)


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"{cmd}\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def c(port, db="postgres"):
    x = psycopg2.connect(host="127.0.0.1", port=port, dbname=db, user="postgres", connect_timeout=3)
    x.autocommit = True
    return x


def q(port, db, sql, args=None):
    x = c(port, db)
    cur = x.cursor()
    cur.execute(sql, args)
    out = cur.fetchall() if cur.description else None
    x.close()
    return out


def explain(port, db, sql, analyze=False):
    rows = q(port, db, ("EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF) " if analyze else "EXPLAIN (VERBOSE, COSTS OFF) ") + sql)
    return [r[0] for r in rows]


def ms(port, db, sql, n=5):
    x = c(port, db)
    cur = x.cursor()
    ts = []
    for _ in range(n):
        t = time.perf_counter()
        cur.execute(sql)
        cur.fetchall()
        ts.append((time.perf_counter() - t) * 1000)
    x.close()
    ts.sort()
    return ts[len(ts) // 2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-port", type=int, default=5433)
    a = ap.parse_args()
    if os.geteuid() == 0:
        sys.exit("run as a normal user")

    shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(BASE)
    for name, port in (("shard1", S1), ("shard2", S2)):
        sh(f"{PGBIN}/initdb -U postgres -D {BASE}/{name} -A trust >/dev/null")
        with open(f"{BASE}/{name}/postgresql.conf", "a") as f:
            f.write(f"\nport = {port}\nunix_socket_directories = '{BASE}'\nlisten_addresses = '127.0.0.1'\n")
        sh(f'{PGBIN}/pg_ctl -D {BASE}/{name} -l {BASE}/{name}.log -w start >/dev/null')
        q(port, "postgres", "CREATE DATABASE shard")
        q(port, "shard", """CREATE TABLE event_shard (
              event_id bigint PRIMARY KEY, match_id int NOT NULL, team_id int NOT NULL, player_id int NOT NULL,
              minute smallint NOT NULL, second smallint NOT NULL, event_type text NOT NULL, x numeric(4,1) NOT NULL,
              y numeric(4,1) NOT NULL, end_x numeric(4,1), end_y numeric(4,1), successful boolean NOT NULL, receiver_id int)""")
        q(port, "shard", "CREATE INDEX ON event_shard (match_id)")
    say(f"[setup] two independent PostgreSQL nodes started: shard1 :{S1}, shard2 :{S2}")

    # coordinator database
    q(a.src_port, "postgres", "DROP DATABASE IF EXISTS coordinator")
    q(a.src_port, "postgres", "CREATE DATABASE coordinator")
    D = "coordinator"
    q(a.src_port, D, "CREATE EXTENSION postgres_fdw")
    for n, port in (("shard1", S1), ("shard2", S2)):
        q(a.src_port, D, f"CREATE SERVER {n} FOREIGN DATA WRAPPER postgres_fdw OPTIONS (host '127.0.0.1', port '{port}', dbname 'shard', batch_size '1000', fetch_size '10000')")
        q(a.src_port, D, f"CREATE USER MAPPING FOR postgres SERVER {n} OPTIONS (user 'postgres')")
    q(a.src_port, D, f"CREATE SERVER src FOREIGN DATA WRAPPER postgres_fdw OPTIONS (host '127.0.0.1', port '{a.src_port}', dbname 'dataxi', fetch_size '10000')")
    q(a.src_port, D, "CREATE USER MAPPING FOR postgres SERVER src OPTIONS (user 'postgres')")
    q(a.src_port, D, """CREATE TABLE event_sharded (
              event_id bigint NOT NULL, match_id int NOT NULL, team_id int NOT NULL, player_id int NOT NULL,
              minute smallint NOT NULL, second smallint NOT NULL, event_type text NOT NULL, x numeric(4,1) NOT NULL,
              y numeric(4,1) NOT NULL, end_x numeric(4,1), end_y numeric(4,1), successful boolean NOT NULL, receiver_id int
          ) PARTITION BY HASH (match_id)""")
    q(a.src_port, D, f"CREATE FOREIGN TABLE event_s1 PARTITION OF event_sharded FOR VALUES WITH (MODULUS 2, REMAINDER 0) SERVER shard1 OPTIONS (table_name 'event_shard')")
    q(a.src_port, D, f"CREATE FOREIGN TABLE event_s2 PARTITION OF event_sharded FOR VALUES WITH (MODULUS 2, REMAINDER 1) SERVER shard2 OPTIONS (table_name 'event_shard')")
    q(a.src_port, D, "IMPORT FOREIGN SCHEMA dataxi LIMIT TO (event, match) FROM SERVER src INTO public")
    q(a.src_port, D, "ALTER FOREIGN TABLE event RENAME TO src_event")
    q(a.src_port, D, "CREATE TABLE match_local AS SELECT * FROM match")          # dimension data stays local to the coordinator
    q(a.src_port, D, "ANALYZE match_local")

    say("\n=== 1. Loading through the coordinator: rows are routed by hash(match_id) ===")
    t = time.time()
    q(a.src_port, D, "INSERT INTO event_sharded SELECT * FROM src_event")
    say(f"inserted 1,140,000 events through event_sharded in {time.time() - t:.0f}s (each row sent to the shard that owns its match)")
    n1 = q(S1, "shard", "SELECT count(*), count(DISTINCT match_id) FROM event_shard")[0]
    n2 = q(S2, "shard", "SELECT count(*), count(DISTINCT match_id) FROM event_shard")[0]
    say(f"shard1 holds {n1[0]:,} events from {n1[1]} matches | shard2 holds {n2[0]:,} events from {n2[1]} matches")
    say(f"balance: {abs(n1[0] - n2[0]) / (n1[0] + n2[0]) * 100:.1f}% difference between shards (hash distribution)")
    both = q(a.src_port, D, "SELECT count(*) FROM event_sharded")[0][0]
    say(f"count through the coordinator = {both:,}  (matches the source table: {q(a.src_port, 'dataxi', 'SELECT count(*) FROM dataxi.event')[0][0]:,})")

    say("\n=== 2. Shard pruning: a query that includes the shard key touches ONE node ===")
    for l in explain(a.src_port, D, "SELECT count(*) FROM event_sharded WHERE match_id = 951"):
        say("   " + l)
    say("\n   ...but a query WITHOUT the shard key must ask both nodes (scatter-gather):")
    for l in explain(a.src_port, D, "SELECT count(*) FROM event_sharded WHERE player_id = 417"):
        say("   " + l)

    say("\n=== 3. Latency: sharded vs the single-node table ===")
    t_single_point = ms(a.src_port, "dataxi", "SELECT count(*) FROM dataxi.event WHERE match_id = 951")
    t_shard_point = ms(a.src_port, D, "SELECT count(*) FROM event_sharded WHERE match_id = 951")
    t_single_scan = ms(a.src_port, "dataxi", "SELECT event_type, count(*) FROM dataxi.event GROUP BY 1")
    q(a.src_port, D, "SET enable_partitionwise_aggregate = off")
    t_shard_scan = ms(a.src_port, D, "SELECT event_type, count(*) FROM event_sharded GROUP BY 1")
    x = c(a.src_port, D); cur = x.cursor(); cur.execute("SET enable_partitionwise_aggregate = on")
    ts = []
    for _ in range(5):
        t0 = time.perf_counter(); cur.execute("SELECT event_type, count(*) FROM event_sharded GROUP BY 1"); cur.fetchall(); ts.append((time.perf_counter() - t0) * 1000)
    ts.sort(); t_shard_scan_pw = ts[2]
    x.close()
    say(f"point lookup (one match):    single node {t_single_point:6.2f} ms | sharded via coordinator {t_shard_point:6.2f} ms   (extra network hop, same work)")
    say(f"full aggregate (all events): single node {t_single_scan:6.0f} ms | sharded, one aggregate at the coordinator {t_shard_scan:6.0f} ms | sharded, per-partition partial aggregates {t_shard_scan_pw:6.0f} ms")
    say("   -> postgres_fdw (PG16) only pushes FILTERS to the shards; for GROUP BY every needed column is shipped to the coordinator, so a full scan is far slower than on one node.")
    say("      Sharding is not automatically faster. The gain is capacity (data bigger than one node) and keeping every per-match query on one shard.")

    say("\n=== 4. A cross-shard join with local dimension data ===")
    for l in explain(a.src_port, D, "SELECT m.matchday, count(*) FROM event_sharded e JOIN match_local m USING (match_id) WHERE m.cs_id = 3 AND m.matchday = 10 GROUP BY 1"):
        say("   " + l)

    say("\n=== 5. Partial availability: shard2 goes down ===")
    sh(f"{PGBIN}/pg_ctl -D {BASE}/shard2 -m fast -w stop >/dev/null")
    m1 = q(S1, "shard", "SELECT match_id FROM event_shard LIMIT 1")[0][0]
    ok = q(a.src_port, D, "SELECT count(*) FROM event_sharded WHERE match_id = %s", (m1,))[0][0]
    say(f"match {m1} (lives on shard1): {ok:,} events returned normally")
    try:
        q(a.src_port, D, "SELECT count(*) FROM event_sharded")
    except Exception as e:
        say(f"query over all matches -> {type(e).__name__}: {str(e).strip().splitlines()[0]}")
    sh(f'{PGBIN}/pg_ctl -D {BASE}/shard2 -l {BASE}/shard2.log -w start >/dev/null')
    say("   -> only the matches on the failed shard are unavailable; with a replica per shard (Part 5a) even those survive.")

    say("\n=== 6. The cost of adding a third shard (rebalancing) ===")
    # PostgreSQL's hash-partition rule: remainder = (hashint4extended(key, seed) + 0x49a0f4dd15e5a8e3) mod 2^64, then mod MODULUS.
    HV = ("((hashint4extended(match_id, 8816678312871386365)::numeric + CASE WHEN hashint4extended(match_id, 8816678312871386365) < 0 "
          "THEN 18446744073709551616 ELSE 0 END + 5305509591434766563) % 18446744073709551616)")
    r0, r1, moved = q(a.src_port, D, f"SELECT count(*) FILTER (WHERE hv % 2 = 0), count(*) FILTER (WHERE hv % 2 = 1), count(*) FILTER (WHERE hv % 3 <> hv % 2) "
                                      f"FROM (SELECT {HV} AS hv FROM (SELECT DISTINCT match_id FROM event_sharded) d) h")[0]
    native = q(a.src_port, D, "SELECT count(*) FILTER (WHERE satisfies_hash_partition('event_sharded'::regclass, 2, 0, match_id)) FROM (SELECT DISTINCT match_id FROM event_sharded) d")[0][0]
    say(f"placement rule hash(match_id) mod 2: my formula puts {r0} matches on shard1 and {r1} on shard2; PostgreSQL's own satisfies_hash_partition() says {native} on shard1; actual {n1[1]} / {n2[1]}")
    say(f"   -> formula {'verified against the real placement' if (r0, r1) == (n1[1], n2[1]) and native == r0 else 'DOES NOT MATCH'}")
    say(f"going from 2 to 3 shards by hash-mod changes the owner of {moved} of 1,140 matches ({moved / 1140 * 100:.0f}%) -> a large data migration. Consistent hashing or range sharding by match_id would move far fewer.")

    say("\n=== 7. What sharding costs you (design limits seen above) ===")
    say("   - no global primary key or foreign keys across shards: event_id is only unique within a shard (PostgreSQL cannot enforce it across foreign partitions)")
    say("   - queries must carry the shard key (match_id) or they fan out to every node")
    say("   - cross-shard transactions would need two-phase commit; this design avoids them because one match = one shard")

    for n in ("shard1", "shard2"):
        subprocess.run(f"{PGBIN}/pg_ctl -D {BASE}/{n} -m fast -w stop >/dev/null", shell=True)
    with open(os.path.join(os.environ.get("RESULTS_DIR", os.path.join(HERE, "..", "results")), "sharding_demo.log"), "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
