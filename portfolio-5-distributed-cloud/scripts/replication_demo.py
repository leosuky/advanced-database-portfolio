#!/usr/bin/env python3
"""
Portfolio 5a - replication, consistency and failover, on real PostgreSQL servers.

What it builds (all on localhost, in a scratch folder):

    ha_primary  :5441   a clone of the dataxi database (taken with pg_basebackup)
    ha_replica  :5442   a streaming replica of ha_primary (hot standby: readable, not writable)

What it demonstrates, in order
    1  replica is read-only; reads can be scaled out
    2  replication lag under a write burst (asynchronous replication)
    3  synchronous replication: stronger consistency, but commits wait for the replica
    4  availability vs consistency: synchronous primary with the replica DOWN (commits block)
    5  failover with ASYNC replication: an acknowledged commit can be lost (data-loss window, RPO > 0)
    6  failover with SYNC replication: nothing acknowledged is lost
    7  the old primary comes back and must be rebuilt as a replica (no split brain)

Requirements: PostgreSQL 16 binaries. Set PGBIN (default /usr/lib/postgresql/16/bin; on macOS with
Homebrew: export PGBIN="$(brew --prefix postgresql@16)/bin"). Must NOT be run as root.
The source cluster must be running (default 127.0.0.1:5433) with wal_level=replica and a 'dataxi' DB.
    python replication_demo.py --src-port 5433
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
BASE = os.environ.get("HA_BASE", "/tmp/ads_ha")
PRIM, REPL = 5441, 5442
lines = []


def say(m=""):
    print(m, flush=True)
    lines.append(m)


def sh(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd}\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def conn(port, autocommit=True):
    c = psycopg2.connect(host="127.0.0.1", port=port, dbname="dataxi", user="postgres",
                         options="-c search_path=dataxi,public", connect_timeout=3)
    c.autocommit = autocommit
    return c


def q(port, sql, args=None):
    c = conn(port)
    cur = c.cursor()
    cur.execute(sql, args)
    out = cur.fetchall() if cur.description else None
    c.close()
    return out


def up(port):
    try:
        conn(port).close()
        return True
    except Exception:
        return False


def start(name, port):
    sh(f'{PGBIN}/pg_ctl -D {BASE}/{name} -o "-p {port} -k {BASE}" -l {BASE}/{name}.log -w start')


def stop(name, mode="fast"):
    sh(f"{PGBIN}/pg_ctl -D {BASE}/{name} -m {mode} -w stop", check=False)


def wait_replica_caught_up(timeout=30):
    t = time.time()
    while time.time() - t < timeout:
        r = q(PRIM, "SELECT count(*) FROM pg_stat_replication WHERE state='streaming' AND replay_lsn = sent_lsn")[0][0]
        if r == 1:
            return True
        time.sleep(0.2)
    return False


def build_pair(src_port):
    shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(BASE)
    say(f"[setup] cloning the dataxi cluster on :{src_port} into a new primary on :{PRIM} with pg_basebackup ...")
    sh(f"{PGBIN}/pg_basebackup -h 127.0.0.1 -p {src_port} -U postgres -D {BASE}/ha_primary -X stream -c fast")
    with open(f"{BASE}/ha_primary/postgresql.conf", "a") as f:
        f.write(f"\nport = {PRIM}\nunix_socket_directories = '{BASE}'\nlisten_addresses = '127.0.0.1'\n"
                "wal_level = replica\nmax_wal_senders = 10\nhot_standby = on\n")
    start("ha_primary", PRIM)
    say(f"[setup] streaming replica on :{REPL} (pg_basebackup -R writes standby.signal + primary_conninfo)")
    sh(f'{PGBIN}/pg_basebackup -d "host=127.0.0.1 port={PRIM} user=postgres application_name=replica1" -D {BASE}/ha_replica -X stream -R -c fast')
    with open(f"{BASE}/ha_replica/postgresql.conf", "a") as f:
        f.write(f"\nport = {REPL}\nunix_socket_directories = '{BASE}'\n")
    start("ha_replica", REPL)
    q(PRIM, "CREATE TABLE IF NOT EXISTS dataxi.ha_marker (id serial PRIMARY KEY, note text, created_at timestamptz DEFAULT now())")
    wait_replica_caught_up()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-port", type=int, default=5433)
    a = ap.parse_args()
    if os.geteuid() == 0:
        sys.exit("run this as a normal user (PostgreSQL refuses to run as root)")

    build_pair(a.src_port)

    # ------------------------------------------------------------------ 1
    say("\n=== 1. The replica is a read-only copy ===")
    say(f"pg_is_in_recovery(): primary={q(PRIM, 'SELECT pg_is_in_recovery()')[0][0]}  replica={q(REPL, 'SELECT pg_is_in_recovery()')[0][0]}")
    say(f"matches on primary={q(PRIM, 'SELECT count(*) FROM match')[0][0]}  replica={q(REPL, 'SELECT count(*) FROM match')[0][0]}   events replica={q(REPL, 'SELECT count(*) FROM event')[0][0]}")
    try:
        q(REPL, "INSERT INTO ha_marker (note) VALUES ('write to replica')")
    except Exception as e:
        say(f"write on replica -> {type(e).__name__}: {str(e).strip().splitlines()[0]}")
    say("read scaling: the API's read-only queries can be pointed at the replica (host/port change only):")
    say(f"   top scorer via replica = {q(REPL, 'SELECT p.full_name, sum(pm.goals) g FROM player_match pm JOIN player p USING (player_id) GROUP BY p.player_id, p.full_name ORDER BY g DESC, p.player_id LIMIT 1')[0]}")
    say("replication status as seen from the primary (pg_stat_replication):")
    r = q(PRIM, "SELECT application_name, state, sync_state FROM pg_stat_replication")[0]
    say(f"   application_name={r[0]} state={r[1]} sync_state={r[2]}")

    # ------------------------------------------------------------------ 2
    say("\n=== 2. Asynchronous replication: lag under a write burst ===")
    c = conn(PRIM)
    cur = c.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS lag_probe (id bigserial PRIMARY KEY, payload text)")
    t = time.time()
    cur.execute("INSERT INTO lag_probe (payload) SELECT repeat('x', 200) FROM generate_series(1, 400000)")
    t_commit = time.time() - t
    cur.execute("SELECT pg_current_wal_lsn()")
    lsn = cur.fetchone()[0]
    r_count_early = q(REPL, "SELECT count(*) FROM lag_probe")[0][0]
    cur.execute("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) FROM pg_stat_replication")
    behind = cur.fetchone()[0]
    say(f"primary committed 400,000 rows in {t_commit:.2f}s. Replica could see {r_count_early:,} of them immediately after; it was {behind/1024:.0f} KiB of WAL behind.")
    wait_replica_caught_up()
    say(f"a moment later the replica shows {q(REPL, 'SELECT count(*) FROM lag_probe')[0][0]:,} rows (it caught up) -> the replica is EVENTUALLY consistent")
    c.close()

    # ------------------------------------------------------------------ 3
    say("\n=== 3. Synchronous replication: commit waits for the replica ===")
    def commit_ms(n=60):
        c = conn(PRIM)
        cur = c.cursor()
        ts = []
        for i in range(n):
            t0 = time.perf_counter()
            cur.execute("INSERT INTO ha_marker (note) VALUES ('latency probe')")
            ts.append((time.perf_counter() - t0) * 1000)
        c.close()
        ts.sort()
        return ts[len(ts) // 2]

    q(PRIM, "ALTER SYSTEM SET synchronous_commit = 'local'"); q(PRIM, "SELECT pg_reload_conf()")
    time.sleep(0.3)
    m_async = commit_ms()
    q(PRIM, "ALTER SYSTEM SET synchronous_standby_names = 'replica1'")
    q(PRIM, "ALTER SYSTEM SET synchronous_commit = 'remote_apply'")
    q(PRIM, "SELECT pg_reload_conf()")
    time.sleep(0.5)
    m_sync = commit_ms()
    r = q(PRIM, "SELECT sync_state FROM pg_stat_replication")[0][0]
    say(f"median commit time, 60 single-row inserts:  async ('local') = {m_async:.2f} ms   |   sync ('remote_apply', sync_state={r}) = {m_sync:.2f} ms")
    c = conn(PRIM); cur = c.cursor()
    cur.execute("INSERT INTO ha_marker (note) VALUES ('read-your-write')")
    cur.execute("SELECT id FROM ha_marker WHERE note='read-your-write'"); mid = cur.fetchone()[0]
    say(f"read-your-writes check: row {mid} was acknowledged by the primary, and the replica already shows it: {bool(q(REPL, 'SELECT 1 FROM ha_marker WHERE id=%s', (mid,)))}")
    c.close()

    # ------------------------------------------------------------------ 4
    say("\n=== 4. Consistency vs availability: synchronous primary, replica goes DOWN ===")
    stop("ha_replica")
    say("replica stopped (simulates a network partition / node failure)")
    # statement_timeout does NOT interrupt a commit that is waiting for a synchronous standby, so we run the insert
    # in a thread and, after 4 seconds, cancel it from another session (what an operator or a health-check would do).
    import threading
    result = {}

    def blocked_insert():
        c2 = conn(PRIM)
        result["pid"] = c2.get_backend_pid()
        t1 = time.time()
        cur2 = c2.cursor()
        cur2.execute("INSERT INTO ha_marker (note) VALUES ('while replica down, sync mode')")
        result["secs"] = time.time() - t1
        result["notices"] = [n.strip() for n in c2.notices]
        c2.close()

    th = threading.Thread(target=blocked_insert)
    th.start()
    time.sleep(4)
    waiting = q(PRIM, "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s", (result["pid"],))[0]
    say(f"after 4 s the insert is still not acknowledged; pg_stat_activity says the session is waiting on {waiting[0]}/{waiting[1]}")
    say("-> sync replication chooses CONSISTENCY over availability: the primary will not acknowledge a write nobody else has (CP behaviour)")
    q(PRIM, "SELECT pg_cancel_backend(%s)", (result["pid"],))
    th.join(timeout=5)
    say(f"operator cancels the wait; Postgres replies: {result['notices'][0] if result.get('notices') else '(no notice)'}")
    say(f"   the row is now committed LOCALLY only (visible on the primary: {bool(q(PRIM, 'SELECT 1 FROM ha_marker WHERE note=%s', ('while replica down, sync mode',)))}) but was never replicated")
    q(PRIM, "ALTER SYSTEM SET synchronous_standby_names = ''")
    q(PRIM, "ALTER SYSTEM SET synchronous_commit = 'local'"); q(PRIM, "SELECT pg_reload_conf()")
    time.sleep(0.3)
    t0 = time.time()
    q(PRIM, "INSERT INTO ha_marker (note) VALUES ('while replica down, async mode')")
    say(f"after switching back to async: the same insert returns in {(time.time() - t0) * 1000:.1f} ms -> async chooses AVAILABILITY (AP-leaning), but the replica is now missing data")
    start("ha_replica", REPL)
    wait_replica_caught_up()
    say("replica restarted and caught up from the primary's WAL")

    # ------------------------------------------------------------------ 5
    say("\n=== 5. Failover with ASYNC replication: can an acknowledged commit be lost? ===")
    stop("ha_replica")
    say("network partition: replica is unreachable")
    c = conn(PRIM); cur = c.cursor()
    cur.execute("INSERT INTO ha_marker (note) VALUES ('ACKNOWLEDGED-BUT-NOT-REPLICATED')")
    say("primary ACKNOWLEDGED the commit to the client (async mode, nobody waiting)")
    c.close()
    stop("ha_primary", "immediate")
    say("primary CRASHES (pg_ctl stop -m immediate)")
    # bring the replica back up standalone and promote it
    start("ha_replica", REPL)
    sh(f"{PGBIN}/pg_ctl -D {BASE}/ha_replica -w promote")
    time.sleep(1)
    say(f"replica PROMOTED: pg_is_in_recovery() = {q(REPL, 'SELECT pg_is_in_recovery()')[0][0]}  (it is the new primary and accepts writes)")
    lost = q(REPL, "SELECT count(*) FROM ha_marker WHERE note='ACKNOWLEDGED-BUT-NOT-REPLICATED'")[0][0]
    say(f"is the acknowledged row on the new primary? {'yes' if lost else 'NO - it was lost (data-loss window: RPO > 0 for async replication)'}")
    q(REPL, "INSERT INTO ha_marker (note) VALUES ('first write on new primary')")

    # ------------------------------------------------------------------ 7  (old primary returns)
    say("\n=== 6. The old primary comes back: it has a write the new primary never saw ===")
    start("ha_primary", PRIM)
    say(f"old primary restarted on :{PRIM}; it still thinks it is primary (pg_is_in_recovery={q(PRIM, 'SELECT pg_is_in_recovery()')[0][0]})")
    say(f"   old primary has the lost row: {q(PRIM, 'SELECT count(*) FROM ha_marker WHERE note=%s', ('ACKNOWLEDGED-BUT-NOT-REPLICATED',))[0][0]}   new primary has it: {lost}")
    say("   => TWO nodes each believe they are primary with DIFFERENT data = split brain. Real systems prevent it by")
    say("      fencing the old primary (STONITH / cloud API) and rebuilding it as a replica with pg_rewind or a fresh pg_basebackup.")
    stop("ha_primary")

    # ------------------------------------------------------------------ 8  sync failover
    say("\n=== 7. Same failure with SYNCHRONOUS replication: nothing acknowledged is lost ===")
    # rebuild old primary as a replica of the new primary (the correct recovery procedure)
    shutil.rmtree(f"{BASE}/ha_primary", ignore_errors=True)
    sh(f'{PGBIN}/pg_basebackup -d "host=127.0.0.1 port={REPL} user=postgres application_name=replica1" -D {BASE}/ha_primary -X stream -R -c fast')
    with open(f"{BASE}/ha_primary/postgresql.conf", "a") as f:
        f.write(f"\nport = {PRIM}\nunix_socket_directories = '{BASE}'\n")
    start("ha_primary", PRIM)
    say(f"old primary REBUILT as a replica of :{REPL}  (pg_is_in_recovery={q(PRIM, 'SELECT pg_is_in_recovery()')[0][0]})")
    q(REPL, "ALTER SYSTEM SET synchronous_standby_names = 'replica1'"); q(REPL, "ALTER SYSTEM SET synchronous_commit = 'remote_apply'")
    q(REPL, "SELECT pg_reload_conf()")
    time.sleep(1)
    c = psycopg2.connect(host="127.0.0.1", port=REPL, dbname="dataxi", user="postgres", options="-c search_path=dataxi,public")
    c.autocommit = True; cur = c.cursor()
    cur.execute("INSERT INTO ha_marker (note) VALUES ('ACKNOWLEDGED-WITH-SYNC')")
    say(f"acknowledged commit on current primary :{REPL} (sync_state={q(REPL, 'SELECT sync_state FROM pg_stat_replication')[0][0]})")
    c.close()
    stop("ha_replica", "immediate")
    say(f"current primary CRASHES. Promote the standby on :{PRIM} ...")
    sh(f"{PGBIN}/pg_ctl -D {BASE}/ha_primary -w promote")
    time.sleep(1)
    say(f"row present on the promoted standby: {bool(q(PRIM, 'SELECT 1 FROM ha_marker WHERE note=%s', ('ACKNOWLEDGED-WITH-SYNC',)))}   (RPO = 0)")

    stop("ha_primary")
    with open(os.path.join(os.environ.get("RESULTS_DIR", os.path.join(HERE, "..", "results")), "replication_demo.log"), "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
