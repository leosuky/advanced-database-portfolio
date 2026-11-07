#!/usr/bin/env python3
"""
Portfolio 3 - transactions and concurrency, run against the dataxi database.

Each scenario prints a timeline of what every session did and saw, and the whole
run is also saved to ../results/scenarios.log.

    python run_scenarios.py --dsn "host=127.0.0.1 port=5433 dbname=dataxi user=postgres"

Scenarios
    1  Atomic commit      - correct a match result: match + 2 standings rows + audit row move together
    2  Rollback/savepoint - a failing step undoes everything; a savepoint undoes only part
    3  Isolation levels   - non-repeatable read and phantom under READ COMMITTED vs REPEATABLE READ
    4  Lost update        - two workers read-modify-write the same row; two fixes
    5  Deadlock           - opposite lock order -> Postgres aborts one; consistent order fixes it
    6  Write skew         - REPEATABLE READ allows it, SERIALIZABLE (SSI) prevents it
"""
import argparse
import os
import threading
import time

import psycopg2
from psycopg2 import errors, extensions

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "..", "results", "scenarios.log")
T0 = time.time()
_lock = threading.Lock()
_log_lines = []


def say(who, msg):
    line = f"[{time.time() - T0:6.2f}s] {who:<4} {msg}"
    with _lock:
        print(line, flush=True)
        _log_lines.append(line)


def banner(txt):
    with _lock:
        print(f"\n{'=' * 78}\n{txt}\n{'=' * 78}", flush=True)
        _log_lines.append(f"\n{'=' * 78}\n{txt}\n{'=' * 78}")


class DB:
    def __init__(self, dsn):
        self.dsn = dsn

    def conn(self, iso=None, autocommit=False):
        # search_path is passed as a connection option so a ROLLBACK cannot undo it
        c = psycopg2.connect(self.dsn, options="-c search_path=dataxi,public")
        c.set_session(isolation_level=iso, autocommit=autocommit)
        return c

    def one(self, sql, args=None):
        c = self.conn(autocommit=True)
        cur = c.cursor()
        cur.execute(sql, args)
        r = cur.fetchone()
        c.close()
        return r

    def run(self, sql, args=None):
        c = self.conn(autocommit=True)
        c.cursor().execute(sql, args)
        c.close()


def reset(db, match_id=None):
    """Put the database back to a known state before each scenario."""
    db.run("TRUNCATE result_audit, ingestion_job RESTART IDENTITY")
    # restore any scores the previous scenario changed, then rebuild the derived table
    db.run("UPDATE match SET home_goals = %s, away_goals = %s WHERE match_id = %s", (*ORIGINAL[match_id], match_id)) if match_id else None
    db.run("SELECT rebuild_standings(3::smallint)")


def standing(cur, team):
    cur.execute("SELECT played, won, drawn, lost, points FROM standings WHERE cs_id = 3 AND team_id = %s", (team,))
    return cur.fetchone()


# --------------------------------------------------------------------------- scenarios
def s1_atomic_commit(db, m):
    banner("SCENARIO 1 - Atomic commit: correct a match result (A, C and D of ACID)")
    mid, h, a, hg, ag = m
    say("T1", f"match {mid}: team {h} v team {a}, recorded score {hg}-{ag}. VAR review changes it to {hg}-{hg} (a draw).")
    c = db.conn()
    cur = c.cursor()
    say("T1", f"BEFORE  standings team {h} (played,won,drawn,lost,pts) = {standing(cur, h)}   team {a} = {standing(cur, a)}")
    cur.execute("SELECT correct_result(%s, %s, %s, %s)", (mid, hg, hg, "VAR review: goal disallowed"))
    say("T1", "correct_result() ran inside the open transaction (not yet committed)")
    other = db.conn(autocommit=True).cursor()
    other.execute("SELECT home_goals, away_goals FROM match WHERE match_id = %s", (mid,))
    say("T2", f"another session still sees the OLD score before commit: {other.fetchone()}   (isolation)")
    c.commit()
    say("T1", "COMMIT")
    cur.execute("SELECT home_goals, away_goals FROM match WHERE match_id = %s", (mid,))
    say("T1", f"AFTER   score {cur.fetchone()}  standings team {h} = {standing(cur, h)}   team {a} = {standing(cur, a)}")
    cur.execute("SELECT old_home_goals, old_away_goals, new_home_goals, new_away_goals, reason FROM result_audit")
    say("T1", f"audit row written in the same transaction: {cur.fetchall()}")
    cur.execute("SELECT count(*) FROM standings_drift")
    say("T1", f"standings vs fresh recomputation: {cur.fetchone()[0]} drifting rows (consistency)")
    c.close()


def s2_rollback_and_savepoint(db, m):
    banner("SCENARIO 2 - Rollback and savepoints (atomicity when something fails halfway)")
    mid, h, a, hg, ag = m
    c = db.conn()
    cur = c.cursor()
    say("T1", f"BEFORE standings team {h} = {standing(cur, h)}")
    say("T1", "step 1: correct_result() succeeds ...")
    cur.execute("SELECT correct_result(%s, %s, %s, 'test')", (mid, hg, hg))        # 2-1 win becomes a 2-2 draw
    say("T1", f"   inside tx: team {h} = {standing(cur, h)}")
    say("T1", "step 2: a buggy script now forces an invalid row (points that do not equal 3*won+drawn) ...")
    try:
        cur.execute("UPDATE standings SET points = points + 1 WHERE cs_id = 3 AND team_id = %s", (h,))
    except errors.CheckViolation as e:
        say("T1", f"   REJECTED by CHECK constraint: {e.diag.constraint_name}")
        c.rollback()
        say("T1", "ROLLBACK -> step 1 is undone too")
    cur.execute("SELECT home_goals, away_goals FROM match WHERE match_id = %s", (mid,))
    say("T1", f"AFTER   score {cur.fetchone()}  team {h} = {standing(cur, h)}  audit rows = {db.one('SELECT count(*) FROM result_audit')[0]}")
    c.close()

    say("--", "")
    say("T1", "Savepoint demo: ingest 3 jobs, the middle one is bad. Only the bad one is undone.")
    c = db.conn()
    cur = c.cursor()
    rows = [(1, 1, 120), (1, 999999, 80), (1, 2, 95)]       # middle row references a match that does not exist
    for prov, match_ref, n in rows:
        cur.execute("SAVEPOINT job")
        try:
            cur.execute("INSERT INTO ingestion_job (provider_id, match_id, status, rows_loaded) VALUES (%s,%s,'succeeded',%s)", (prov, match_ref, n))
            cur.execute("RELEASE SAVEPOINT job")
            say("T1", f"job for match {match_ref}: OK")
        except errors.ForeignKeyViolation:
            cur.execute("ROLLBACK TO SAVEPOINT job")
            say("T1", f"job for match {match_ref}: FK violation -> ROLLBACK TO SAVEPOINT (other jobs unaffected)")
    c.commit()
    say("T1", f"COMMIT. Jobs stored: {db.one('SELECT count(*) FROM ingestion_job')[0]} (expected 2 of 3)")
    c.close()


def s3_isolation(db, m):
    banner("SCENARIO 3 - Isolation levels: non-repeatable read and phantom read")
    mid, h, a, hg, ag = m

    for iso, name in ((extensions.ISOLATION_LEVEL_READ_COMMITTED, "READ COMMITTED (Postgres default)"),
                      (extensions.ISOLATION_LEVEL_REPEATABLE_READ, "REPEATABLE READ")):
        reset(db, mid)
        say("--", f"--- {name} ---")
        t1 = db.conn(iso)
        c1 = t1.cursor()
        c1.execute("SELECT points FROM standings WHERE cs_id=3 AND team_id=%s", (h,))
        first = c1.fetchone()[0]
        c1.execute("SELECT count(*) FROM ingestion_job")
        n_first = c1.fetchone()[0]
        say("T1", f"read #1: team {h} points = {first}; ingestion jobs = {n_first}")
        t2 = db.conn(autocommit=True).cursor()
        t2.execute("SELECT correct_result(%s,%s,%s,'concurrent edit')", (mid, hg, hg))   # win -> draw: points change
        t2.execute("INSERT INTO ingestion_job (provider_id, match_id, status) VALUES (1, %s, 'succeeded')", (mid,))
        say("T2", "committed: corrected a score (changes team points) and inserted one new ingestion job")
        c1.execute("SELECT points FROM standings WHERE cs_id=3 AND team_id=%s", (h,))
        second = c1.fetchone()[0]
        c1.execute("SELECT count(*) FROM ingestion_job")
        n_second = c1.fetchone()[0]
        say("T1", f"read #2: team {h} points = {second}; ingestion jobs = {n_second}")
        say("T1", f"  non-repeatable read? {'YES' if first != second else 'no'}   phantom row? {'YES' if n_first != n_second else 'no'}")
        t1.rollback()
        t1.close()

    reset(db, mid)
    say("--", "--- REPEATABLE READ and a write that conflicts with a newer commit ---")
    t1 = db.conn(extensions.ISOLATION_LEVEL_REPEATABLE_READ)
    c1 = t1.cursor()
    c1.execute("SELECT points FROM standings WHERE cs_id=3 AND team_id=%s", (h,))
    say("T1", f"snapshot taken, points = {c1.fetchone()[0]}")
    db.run("UPDATE standings SET played=played WHERE cs_id=3 AND team_id=%s", (h,))
    say("T2", "committed an UPDATE of the same standings row")
    try:
        c1.execute("UPDATE standings SET played=played WHERE cs_id=3 AND team_id=%s", (h,))
    except errors.SerializationFailure as e:
        say("T1", f"UPDATE refused: {str(e).strip().splitlines()[0]}  -> application must retry the transaction")
    t1.rollback()
    t1.close()

    say("--", "--- 'Dirty read' check: READ UNCOMMITTED request ---")
    t1 = db.conn()
    c1 = t1.cursor()
    c1.execute("SELECT correct_result(%s,%s,%s,'uncommitted')", (mid, hg + 4, ag))
    t2 = db.conn(extensions.ISOLATION_LEVEL_READ_UNCOMMITTED)
    c2 = t2.cursor()
    c2.execute("SHOW transaction_isolation")
    lvl = c2.fetchone()[0]
    c2.execute("SELECT home_goals FROM match WHERE match_id=%s", (mid,))
    say("T2", f"requested READ UNCOMMITTED (session reports '{lvl}', but Postgres implements it as READ COMMITTED); "
                  f"sees home_goals = {c2.fetchone()[0]} although T1 has uncommitted {hg + 4} -> no dirty read possible")
    t1.rollback(); t2.rollback(); t1.close(); t2.close()


def s4_lost_update(db, m):
    banner("SCENARIO 4 - Lost update: two workers add a win to the same standings row")
    h = m[1]

    def worker(name, mode, barrier, results):
        c = db.conn()
        cur = c.cursor()
        if mode == "naive":                       # read - compute in app - write
            cur.execute("SELECT played, won, points FROM standings WHERE cs_id=3 AND team_id=%s", (h,))
            played, won, pts = cur.fetchone()
            say(name, f"read played={played} won={won} points={pts}")
            barrier.wait()                         # both workers have read the same values
            time.sleep(0.1)
            cur.execute("UPDATE standings SET played=%s, won=%s, points=%s WHERE cs_id=3 AND team_id=%s",
                        (played + 1, won + 1, pts + 3, h))
        elif mode == "for_update":
            cur.execute("SELECT played, won, points FROM standings WHERE cs_id=3 AND team_id=%s FOR UPDATE", (h,))
            played, won, pts = cur.fetchone()
            say(name, f"locked row, read played={played} won={won} points={pts}")
            time.sleep(0.3)                        # the other worker is blocked on the row lock meanwhile
            cur.execute("UPDATE standings SET played=%s, won=%s, points=%s WHERE cs_id=3 AND team_id=%s",
                        (played + 1, won + 1, pts + 3, h))
        else:                                      # atomic
            cur.execute("UPDATE standings SET played=played+1, won=won+1, points=points+3 WHERE cs_id=3 AND team_id=%s", (h,))
        c.commit()
        say(name, "COMMIT")
        c.close()

    for mode, title in (("naive", "A) naive read-modify-write (BUG)"),
                        ("for_update", "B) fix 1: SELECT ... FOR UPDATE"),
                        ("atomic", "C) fix 2: single atomic UPDATE (won = won + 1)")):
        reset(db, m[0])
        before = db.one("SELECT won, points FROM standings WHERE cs_id=3 AND team_id=%s", (h,))
        say("--", f"--- {title}; start won/points = {before}; two workers each add one win (+3 pts) ---")
        barrier = threading.Barrier(2, timeout=3)
        threads = [threading.Thread(target=worker, args=(n, mode, barrier, None)) for n in ("W1", "W2")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        after = db.one("SELECT won, points FROM standings WHERE cs_id=3 AND team_id=%s", (h,))
        say("--", f"final won/points = {after}   expected = {(before[0] + 2, before[1] + 6)}   -> {'CORRECT' if after == (before[0] + 2, before[1] + 6) else 'LOST UPDATE'}")


def s5_deadlock(db, m):
    banner("SCENARIO 5 - Deadlock: two corrections lock the same two teams in opposite order")
    h, a = m[1], m[2]
    reset(db, m[0])

    def tx(name, first, second, gate):
        c = db.conn()
        cur = c.cursor()
        try:
            cur.execute("UPDATE standings SET played=played WHERE cs_id=3 AND team_id=%s", (first,))
            say(name, f"locked team {first}")
            gate.wait()
            time.sleep(0.2)
            say(name, f"now wants team {second} ...")
            cur.execute("UPDATE standings SET played=played WHERE cs_id=3 AND team_id=%s", (second,))
            c.commit()
            say(name, f"COMMIT (locked {first} then {second})")
        except errors.DeadlockDetected as e:
            say(name, f"ABORTED by Postgres: {str(e).strip().splitlines()[0]}")
            c.rollback()
        finally:
            c.close()

    say("--", f"--- A) opposite order: T1 locks {h} then {a}; T2 locks {a} then {h} ---")
    gate = threading.Barrier(2, timeout=3)
    ts = [threading.Thread(target=tx, args=("T1", h, a, gate)), threading.Thread(target=tx, args=("T2", a, h, gate))]
    [t.start() for t in ts]; [t.join() for t in ts]

    say("--", f"--- B) fix: both lock in ascending team_id order ({min(h, a)} then {max(h, a)}) ---")
    gate = threading.Barrier(2, timeout=3)
    lo, hi = sorted((h, a))
    ts = [threading.Thread(target=tx, args=("T1", lo, hi, threading.Barrier(1))), threading.Thread(target=tx, args=("T2", lo, hi, threading.Barrier(1)))]
    [t.start() for t in ts]; [t.join() for t in ts]
    say("--", "both committed: the second transaction simply waited for the first (this is what correct_result() does)")


def s6_write_skew(db, m):
    banner("SCENARIO 6 - Write skew: 'at most 2 ingestion jobs may run per provider'")

    def worker(name, iso, barrier, out):
        c = db.conn(iso)
        cur = c.cursor()
        try:
            cur.execute("SELECT count(*) FROM ingestion_job WHERE provider_id = 1 AND status = 'running'")
            n = cur.fetchone()[0]
            say(name, f"sees {n} running job(s); limit is 2, so starting one more looks safe")
            barrier.wait()
            if n < 2:
                cur.execute("INSERT INTO ingestion_job (provider_id, match_id, status) VALUES (1, %s, 'running')", (m[0] if name == "T1" else m[0] + 1,))
            c.commit()
            say(name, "COMMIT")
        except errors.SerializationFailure as e:
            say(name, f"ABORTED: {str(e).strip().splitlines()[0]} -> retry would see 2 running and refuse")
            c.rollback()
        finally:
            c.close()

    for iso, name in ((extensions.ISOLATION_LEVEL_REPEATABLE_READ, "REPEATABLE READ"),
                      (extensions.ISOLATION_LEVEL_SERIALIZABLE, "SERIALIZABLE")):
        reset(db, m[0])
        db.run("INSERT INTO ingestion_job (provider_id, match_id, status) VALUES (1, %s, 'running')", (m[0] + 5,))
        say("--", f"--- {name}: one job already running, two sessions each try to start another ---")
        barrier = threading.Barrier(2, timeout=3)
        ts = [threading.Thread(target=worker, args=(n, iso, barrier, None)) for n in ("T1", "T2")]
        [t.start() for t in ts]; [t.join() for t in ts]
        running = db.one("SELECT count(*) FROM ingestion_job WHERE provider_id=1 AND status='running'")[0]
        say("--", f"running jobs now = {running}   -> {'LIMIT BROKEN (write skew)' if running > 2 else 'limit respected'}")


ORIGINAL = {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="host=127.0.0.1 port=5433 dbname=dataxi user=postgres")
    args = ap.parse_args()
    db = DB(args.dsn)

    r = db.one("SELECT match_id, home_team_id, away_team_id, home_goals, away_goals FROM match "
               "WHERE cs_id = 3 AND status = 'finished' AND home_goals = 2 AND away_goals = 1 ORDER BY match_id LIMIT 1")
    m = tuple(r)
    ORIGINAL[m[0]] = (m[3], m[4])
    reset(db, m[0])

    s1_atomic_commit(db, m)
    reset(db, m[0]); s2_rollback_and_savepoint(db, m)
    reset(db, m[0]); s3_isolation(db, m)
    reset(db, m[0]); s4_lost_update(db, m)
    reset(db, m[0]); s5_deadlock(db, m)
    reset(db, m[0]); s6_write_skew(db, m)
    reset(db, m[0])
    drift = db.one("SELECT count(*) FROM standings_drift")[0]
    say("--", f"\nFINAL CHECK: database restored, standings drift rows = {drift}")

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w") as f:
        f.write("\n".join(_log_lines) + "\n")


if __name__ == "__main__":
    main()
