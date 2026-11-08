#!/usr/bin/env python3
"""
Portfolio 4a - Key-value store (Redis) for The Data XI.

Why Redis here: the public website asks the same small questions thousands of times
(season leaderboard, live match score, match page). Those are read-heavy, tiny,
and tolerate being a few seconds stale, so they belong in an in-memory key-value
store in front of PostgreSQL, which stays the source of truth.

    python redis_demo.py --pg "host=127.0.0.1 port=5433 dbname=dataxi user=postgres" --redis-port 6390

Saves its output to ../results/redis_demo.log
"""
import argparse
import json
import os
import statistics
import threading
import time

import psycopg2
import redis

HERE = os.path.dirname(os.path.abspath(__file__))
out_lines = []


def say(msg=""):
    print(msg, flush=True)
    out_lines.append(msg)


def timed(fn, n):
    ts = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t) * 1000)
    return statistics.median(ts), max(ts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg", default="host=127.0.0.1 port=5433 dbname=dataxi user=postgres")
    ap.add_argument("--redis-port", type=int, default=6390)
    a = ap.parse_args()

    pg = psycopg2.connect(a.pg, options="-c search_path=dataxi,public")
    pg.autocommit = True
    cur = pg.cursor()
    r = redis.Redis(port=a.redis_port, decode_responses=True)
    r.flushdb()

    # ---------------------------------------------------------------- 1. sorted set leaderboard
    say("=== 1. SORTED SET: season scorers leaderboard ===")
    cur.execute("""SELECT pm.player_id, p.full_name, sum(pm.goals) FROM player_match pm
                   JOIN match m ON m.match_id = pm.match_id AND m.cs_id = 3
                   JOIN player p ON p.player_id = pm.player_id
                   GROUP BY pm.player_id, p.full_name""")
    rows = cur.fetchall()
    pipe = r.pipeline()
    for pid, name, goals in rows:
        pipe.zadd("lb:scorers:3", {pid: int(goals)})
        pipe.hset(f"player:{pid}", mapping={"name": name})
    pipe.execute()
    say(f"loaded {r.zcard('lb:scorers:3')} players into ZSET lb:scorers:3 (command: ZADD key score member)")
    top = r.zrevrange("lb:scorers:3", 0, 4, withscores=True)
    say("ZREVRANGE lb:scorers:3 0 4 WITHSCORES ->")
    for pid, g in top:
        say(f"    {r.hget(f'player:{pid}', 'name'):<22} {int(g)} goals")
    say(f"rank of top player: ZREVRANK = {r.zrevrank('lb:scorers:3', top[0][0])}   score: ZSCORE = {int(r.zscore('lb:scorers:3', top[0][0]))}")

    def pg_top10():
        cur.execute("""SELECT pm.player_id, sum(pm.goals) g FROM player_match pm
                       JOIN match m ON m.match_id = pm.match_id AND m.cs_id = 3 GROUP BY pm.player_id ORDER BY g DESC LIMIT 10""")
        cur.fetchall()

    def redis_top10():
        r.zrevrange("lb:scorers:3", 0, 9, withscores=True)

    pg_top10(); redis_top10()
    pg_med, pg_max = timed(pg_top10, 300)
    rd_med, rd_max = timed(redis_top10, 300)
    say(f"latency, top-10 scorers, 300 calls from Python: PostgreSQL median {pg_med:.2f} ms (max {pg_max:.1f}) | "
        f"Redis median {rd_med:.3f} ms (max {rd_max:.2f})  -> {pg_med / rd_med:.0f}x")

    # ---------------------------------------------------------------- 2. live score hash + pub/sub
    say("\n=== 2. HASH + PUB/SUB: live match state ===")
    mid = 951
    r.hset(f"live:match:{mid}", mapping={"minute": 0, "home_goals": 0, "away_goals": 0, "status": "live"})
    got = []

    def listener():
        ps = r.pubsub()
        ps.subscribe(f"match:{mid}:events")
        for msg in ps.listen():
            if msg["type"] == "message":
                got.append(msg["data"])
                if len(got) == 2:
                    break

    th = threading.Thread(target=listener)
    th.start()
    time.sleep(0.2)
    for minute, side in ((23, "home_goals"), (67, "away_goals")):
        r.hset(f"live:match:{mid}", "minute", minute)
        new = r.hincrby(f"live:match:{mid}", side, 1)         # atomic increment, no lost update
        r.publish(f"match:{mid}:events", json.dumps({"minute": minute, "scored": side, "now": new}))
    th.join(timeout=3)
    say(f"HGETALL live:match:{mid} -> {r.hgetall(f'live:match:{mid}')}")
    say(f"subscriber received {len(got)} pushed events: {got}")

    # ---------------------------------------------------------------- 3. cache-aside
    say("\n=== 3. STRING + TTL: cache-aside for the match page ===")

    def build_match_page(match_id):
        cur.execute("""SELECT m.match_id, th.canonical_name, ta.canonical_name, m.home_goals, m.away_goals
                       FROM match m JOIN team th ON th.team_id = m.home_team_id JOIN team ta ON ta.team_id = m.away_team_id
                       WHERE m.match_id = %s""", (match_id,))
        row = cur.fetchone()
        cur.execute("SELECT p.full_name, pm.goals, pm.xg FROM player_match pm JOIN player p USING (player_id) WHERE pm.match_id = %s AND pm.goals > 0", (match_id,))
        return {"match_id": row[0], "home": row[1], "away": row[2], "score": f"{row[3]}-{row[4]}",
                "scorers": [{"name": n, "goals": g, "xg": float(x)} for n, g, x in cur.fetchall()]}

    stats = {"hit": 0, "miss": 0}

    def get_match_page(match_id):
        key = f"cache:matchpage:{match_id}"
        v = r.get(key)
        if v:
            stats["hit"] += 1
            return json.loads(v)
        stats["miss"] += 1
        page = build_match_page(match_id)
        r.set(key, json.dumps(page), ex=30)                    # expires in 30 s
        return page

    t = time.perf_counter(); get_match_page(784); cold = (time.perf_counter() - t) * 1000
    t = time.perf_counter(); get_match_page(784); warm = (time.perf_counter() - t) * 1000
    say(f"cold read (miss, queries PostgreSQL): {cold:.2f} ms | warm read (hit): {warm:.3f} ms | TTL left: {r.ttl('cache:matchpage:784')} s")
    for _ in range(98):
        get_match_page(784)
    say(f"after 100 requests: hits={stats['hit']} misses={stats['miss']} hit-rate={stats['hit']}%")

    say("\n-- the price of speed: STALENESS (eventual consistency, BASE) --")
    before = get_match_page(784)["score"]
    cur.execute("SELECT home_goals, away_goals FROM match WHERE match_id = 784")
    hg, ag = cur.fetchone()
    cur.execute("UPDATE match SET away_goals = away_goals + 1 WHERE match_id = 784")
    say(f"PostgreSQL updated 784 to {hg}-{ag + 1}; cached page still says {get_match_page(784)['score']}  (stale until TTL ends or we invalidate)")
    r.delete("cache:matchpage:784")                              # invalidate on write
    say(f"after DEL (cache invalidation on write): page now says {get_match_page(784)['score']}")
    cur.execute("UPDATE match SET away_goals = %s WHERE match_id = 784", (ag,))      # restore original data
    r.delete("cache:matchpage:784")

    # ---------------------------------------------------------------- 4. rate limiter
    say("\n=== 4. INCR + EXPIRE: API rate limit (100 requests per 10 s per key) ===")
    allowed = blocked = 0
    for _ in range(105):
        k = "rl:apikey:demo"
        n = r.incr(k)
        if n == 1:
            r.expire(k, 10)
        if n <= 100:
            allowed += 1
        else:
            blocked += 1
    say(f"105 requests -> allowed {allowed}, blocked {blocked}; counter resets in {r.ttl('rl:apikey:demo')} s")

    say(f"\nmemory used by everything above: {r.info('memory')['used_memory_human']}   keys: {r.dbsize()}")
    with open(os.path.join(HERE, "..", "results", "redis_demo.log"), "w") as f:
        f.write("\n".join(out_lines) + "\n")


if __name__ == "__main__":
    main()
