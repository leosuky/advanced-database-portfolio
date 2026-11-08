#!/usr/bin/env python3
"""
Portfolio 4b - Document store (MongoDB) for The Data XI.

Two collections, two different reasons to use documents:

  match_documents       one self-contained document per match (what a match page needs in ONE read):
                        embeds both line-ups and the shot list. Bounded size (22 players, ~30 shots).
  provider_raw_payloads the raw JSON each data provider returned, kept as-is. Shapes differ per provider
                        and change over time - a schema-on-read problem that suits documents.

Run against a real MongoDB:
    python mongo_demo.py --uri mongodb://localhost:27017
Run the same code against the in-memory mongomock library (what the author used to check the logic
when no mongod was available):
    python mongo_demo.py --mongomock
"""
import argparse
import json
import os
import random
import time

import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
out = []


def say(m=""):
    print(m, flush=True)
    out.append(m)


def build_match_documents(cur):
    cur.execute("""SELECT m.match_id, s.label, m.matchday, m.kickoff_ts, th.team_id, th.canonical_name, ta.team_id, ta.canonical_name,
                          m.home_goals, m.away_goals
                   FROM match m JOIN competition_season cs USING (cs_id) JOIN season s USING (season_id)
                   JOIN team th ON th.team_id = m.home_team_id JOIN team ta ON ta.team_id = m.away_team_id
                   ORDER BY m.match_id""")
    matches = cur.fetchall()
    cur.execute("""SELECT mt.match_id, mt.team_id, mt.xg, mt.shots, mt.possession_pct FROM match_team mt""")
    mt = {(r[0], r[1]): r[2:] for r in cur.fetchall()}
    cur.execute("""SELECT pm.match_id, pm.team_id, pm.player_id, p.full_name, pm.position, pm.minutes_played, pm.goals, pm.assists, pm.xg
                   FROM player_match pm JOIN player p USING (player_id) ORDER BY pm.match_id""")
    lineups = {}
    for r in cur.fetchall():
        lineups.setdefault((r[0], r[1]), []).append({"player_id": r[2], "name": r[3], "pos": r[4], "minutes": r[5],
                                                      "goals": r[6], "assists": r[7], "xg": float(r[8])})
    cur.execute("SELECT match_id, player_id, team_id, minute, xg, outcome, x, y FROM shot ORDER BY match_id, minute")
    shots = {}
    for r in cur.fetchall():
        shots.setdefault(r[0], []).append({"player_id": r[1], "team_id": r[2], "minute": r[3], "xg": float(r[4]),
                                           "outcome": r[5], "x": float(r[6]), "y": float(r[7])})
    docs = []
    for mid, season, md, ko, ht, hn, at, an, hg, ag in matches:
        side = lambda tid, name, g: {"team_id": tid, "name": name, "goals": g, "xg": float(mt[(mid, tid)][0]),
                                      "shots": mt[(mid, tid)][1], "possession": float(mt[(mid, tid)][2]),
                                      "lineup": lineups.get((mid, tid), [])}
        docs.append({"_id": mid, "season": season, "matchday": md, "kickoff": ko, "home": side(ht, hn, hg),
                     "away": side(at, an, ag), "shots": shots.get(mid, [])})
    return docs


def build_provider_payloads(docs, n_matches=200, seed=1):
    """Simulated raw payloads - four providers, four different shapes (no real provider data)."""
    rnd = random.Random(seed)
    out_docs = []
    for d in docs[:n_matches]:
        mid = d["_id"]
        out_docs.append({"provider": "provider_a", "match_id": mid, "fetched_at": d["kickoff"],
                         "payload": {"matchId": 70000 + mid, "events": [
                             {"type": {"displayName": rnd.choice(["Pass", "Tackle", "Clearance"])}, "minute": rnd.randint(0, 90),
                              "x": rnd.random() * 100, "y": rnd.random() * 100} for _ in range(25)]}})
        out_docs.append({"provider": "provider_b", "match_id": mid, "fetched_at": d["kickoff"],
                         "payload": {"general": {"matchId": str(mid), "homeTeam": d["home"]["name"], "awayTeam": d["away"]["name"]},
                                     "stats": {"expectedGoals": {"home": d["home"]["xg"], "away": d["away"]["xg"]}}}})
        out_docs.append({"provider": "provider_c", "match_id": mid, "fetched_at": d["kickoff"],
                         "payload": {"shotmap": [{"playerId": s["player_id"], "expectedGoals": s["xg"], "situation": "OpenPlay"} for s in d["shots"][:10]]}})
        out_docs.append({"provider": "provider_d", "match_id": mid, "fetched_at": d["kickoff"],
                         "payload": {"lineups": {"home": {"formation": "4-3-3", "players": [p["player_id"] for p in d["home"]["lineup"]]},
                                                 "away": {"formation": "4-4-2", "players": [p["player_id"] for p in d["away"]["lineup"]]}},
                                     "schema_version": rnd.choice([1, 2])}})
    return out_docs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg", default="host=127.0.0.1 port=5433 dbname=dataxi user=postgres")
    ap.add_argument("--uri", default="mongodb://localhost:27017")
    ap.add_argument("--mongomock", action="store_true", help="use the in-memory mongomock library instead of a real server")
    a = ap.parse_args()

    if a.mongomock:
        import mongomock
        client = mongomock.MongoClient()
        say("(backend: mongomock - in-memory emulation, NOT a real MongoDB server)")
    else:
        import pymongo
        client = pymongo.MongoClient(a.uri)
        say(f"(backend: real MongoDB at {a.uri}: {client.server_info()['version']})")
    db = client["dataxi"]

    pg = psycopg2.connect(a.pg, options="-c search_path=dataxi,public")
    cur = pg.cursor()
    docs = build_match_documents(cur)
    payloads = build_provider_payloads(docs)

    db.match_documents.drop(); db.provider_raw_payloads.drop()
    db.match_documents.insert_many(docs)
    db.provider_raw_payloads.insert_many(payloads)
    say(f"loaded {db.match_documents.count_documents({})} match documents and {db.provider_raw_payloads.count_documents({})} raw payloads")
    size = len(json.dumps(docs[0], default=str))
    say(f"one match document is about {size / 1024:.1f} KB of JSON (lineups + shots embedded)")

    db.match_documents.create_index([("home.team_id", 1), ("season", 1)])
    db.match_documents.create_index([("away.team_id", 1), ("season", 1)])
    db.match_documents.create_index([("shots.player_id", 1)])               # multikey index over an array
    db.provider_raw_payloads.create_index([("match_id", 1), ("provider", 1)], unique=True)

    say("\n--- M1. point read: the whole match page in ONE document read")
    d = db.match_documents.find_one({"_id": 951}, {"shots": 0})
    say(f"    {d['home']['name']} {d['home']['goals']}-{d['away']['goals']} {d['away']['name']}   "
        f"home line-up size={len(d['home']['lineup'])}   (PostgreSQL needed joins across 5 tables for this)")

    say("\n--- M2. team's fixtures in one season, newest first (uses the compound index)")
    res = list(db.match_documents.find({"$or": [{"home.team_id": 1}, {"away.team_id": 1}], "season": "2025/26"},
                                       {"matchday": 1, "home.name": 1, "away.name": 1, "home.goals": 1, "away.goals": 1}).sort("matchday", -1).limit(3))
    for r in res:
        say(f"    MD{r['matchday']}: {r['home']['name']} {r['home']['goals']}-{r['away']['goals']} {r['away']['name']}")

    say("\n--- M3. aggregation pipeline: top 5 scorers of 2025/26 (unwind line-ups)")
    pipeline = [
        {"$match": {"season": "2025/26"}},
        {"$project": {"players": {"$concatArrays": ["$home.lineup", "$away.lineup"]}}},
        {"$unwind": "$players"},
        {"$group": {"_id": {"id": "$players.player_id", "name": "$players.name"}, "goals": {"$sum": "$players.goals"}}},
        {"$sort": {"goals": -1}}, {"$limit": 5}]
    for r in db.match_documents.aggregate(pipeline):
        say(f"    {r['_id']['name']:<22} {r['goals']}")

    say("\n--- M4. array query ($elemMatch): matches with a stoppage-time goal (after minute 90)")
    n = db.match_documents.count_documents({"shots": {"$elemMatch": {"minute": {"$gt": 90}, "outcome": "goal"}}})
    say(f"    {n} matches")

    say("\n--- M5. schema-on-read: four providers, four shapes, one collection")
    for prov in ("provider_a", "provider_b", "provider_c", "provider_d"):
        sample = db.provider_raw_payloads.find_one({"provider": prov, "match_id": 100})
        say(f"    {prov}: top-level payload keys = {sorted(sample['payload'].keys())}")
    cnt = db.provider_raw_payloads.count_documents({"provider": "provider_d", "payload.schema_version": 2})
    say(f"    provider_d payloads already on schema_version 2: {cnt} (versions can be mixed - no migration needed to ingest)")

    say("\n--- trade-off check: updating a goal in a document vs PostgreSQL")
    say("    one corrected score touches: match_documents.home.goals, away.goals AND the embedded lineup.goals AND the shots list")
    say("    -> the same fact lives in several places; the application (or a change stream) must keep them in step.")
    say("    In PostgreSQL the same correction is one row in `match` (plus the derived standings, Portfolio 3).")

    with open(os.path.join(HERE, "..", "results", "mongo_demo.log"), "w") as f:
        f.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
