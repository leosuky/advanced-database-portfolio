#!/usr/bin/env python3
"""
Generate a SIMULATED football dataset for the MIT 8103 portfolio.

Nothing here comes from a real provider: clubs, players, scores, shots and
events are all invented from a seeded random number generator. That keeps the
portfolio free of third-party data and makes every run reproducible.

Usage:
    python generate_data.py --out ./csv            # default scale (~1.1M events)
    python generate_data.py --out ./csv --events-per-match 200   # small, quick
"""
import argparse
import csv
import os
from datetime import date, datetime, timedelta, timezone

import numpy as np

SEED = 8103
N_TEAMS = 20
SEASONS = [(1, "2023/24", date(2023, 8, 12)),
           (2, "2024/25", date(2024, 8, 17)),
           (3, "2025/26", date(2025, 8, 16))]
POSITIONS = ["GK", "DF", "DF", "DF", "DF", "MF", "MF", "MF", "FW", "FW", "FW"]
EVENT_TYPES = ["pass", "carry", "tackle", "interception", "clearance", "duel", "foul"]
EVENT_P = [0.62, 0.17, 0.05, 0.03, 0.03, 0.07, 0.03]

CITY = ["Northgate", "Eastbourne", "Westfield", "Southmere", "Ashford", "Brookvale", "Calderwick",
        "Dunmore", "Elmstead", "Fairhaven", "Glenmoor", "Harlow", "Ironbridge", "Kingsley",
        "Lakemont", "Marlowe", "Netherby", "Oakhurst", "Pinecrest", "Riverton"]
SUFFIX = ["United", "City", "Rovers", "Athletic", "Town"]
FIRST = ["Tunde", "Chidi", "Emeka", "Sam", "Leo", "Marco", "Jon", "Ade", "Kofi", "Omar", "Ivan", "Luka",
         "Noah", "Ezra", "Hugo", "Mateo", "Idris", "Femi", "Kwame", "Rui", "Dario", "Felix", "Jonas", "Viktor"]
LAST = ["Okafor", "Silva", "Martin", "Haddad", "Bello", "Novak", "Larsen", "Costa", "Mensah", "Petrov",
        "Rossi", "Khan", "Lindgren", "Duarte", "Adeyemi", "Fofana", "Keita", "Moreau", "Santos", "Wright",
        "Ibrahim", "Kovac", "Mendes", "Hansen", "Ortiz", "Diallo", "Weber", "Lopez"]
NATION = ["Nigeria", "Brazil", "France", "Spain", "England", "Ghana", "Portugal", "Croatia", "Senegal", "Norway"]


def double_round_robin(n, rng):
    """Circle-method schedule: every team plays exactly once per matchday, home and away against every opponent.
    Returns [(matchday, home_idx, away_idx), ...] with matchdays 1..2(n-1) in a shuffled order."""
    arr = list(range(n))
    first_half = []
    for r in range(n - 1):
        pairs = []
        for i in range(n // 2):
            h, a = arr[i], arr[n - 1 - i]
            if (r + i) % 2 == 1:
                h, a = a, h                                   # alternate venues so nobody gets long home/away runs
            pairs.append((h, a))
        first_half.append(pairs)
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]                # rotate everything except the first team
    second_half = [[(a, h) for h, a in rnd] for rnd in first_half]   # return fixtures swap venues
    rounds = first_half + second_half
    order = list(range(len(rounds)))
    rng.shuffle(order)
    return [(md + 1, h, a) for md, ri in enumerate(order) for h, a in rounds[ri]]


def w(path, header, rows):
    with open(path, "w", newline="") as f:
        cw = csv.writer(f)
        cw.writerow(header)
        cw.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./csv")
    ap.add_argument("--events-per-match", type=int, default=1000)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rng = np.random.default_rng(SEED)

    # ---- reference data ------------------------------------------------------
    w(f"{a.out}/competition.csv", ["competition_id", "name", "country", "tier"],
      [[1, "Northern Premier Division", "Albion", 1]])
    w(f"{a.out}/season.csv", ["season_id", "label", "start_date", "end_date"],
      [[sid, lab, st, st + timedelta(days=280)] for sid, lab, st in SEASONS])
    w(f"{a.out}/competition_season.csv", ["cs_id", "competition_id", "season_id"],
      [[sid, 1, sid] for sid, _, _ in SEASONS])
    w(f"{a.out}/provider.csv", ["provider_id", "name", "role"],
      [[1, "provider_a", "anchor"], [2, "provider_b", "supplementary"],
       [3, "provider_c", "supplementary"], [4, "provider_d", "supplementary"]])

    teams = []
    for i in range(N_TEAMS):
        teams.append([i + 1, f"{CITY[i]} {SUFFIX[i % 5]}", (CITY[i][:3]).upper(), "Albion",
                      int(rng.integers(1880, 1990))])
    # short codes must be unique (a real collision class the Team Registry had to fix)
    seen = {}
    for t in teams:
        c = t[2]
        while c in seen:
            c = c[:2] + chr(ord(c[2]) + 1)
        seen[c] = 1
        t[2] = c
    w(f"{a.out}/team.csv", ["team_id", "canonical_name", "short_code", "country", "founded_year"], teams)

    tpm = []
    for t in teams:
        for p in range(1, 5):
            tpm.append([p, f"P{p}-{t[0]:04d}", t[0]])
    w(f"{a.out}/team_provider_map.csv", ["provider_id", "provider_team_ref", "team_id"], tpm)

    # team strengths (attack, defence), stable across seasons with slow drift
    attack = rng.normal(1.35, 0.25, N_TEAMS)
    defence = rng.normal(1.35, 0.25, N_TEAMS)

    # ---- players -------------------------------------------------------------
    players, squads = [], {}   # squads[(season, team)] = list of (player_id, pos)
    pid = 0
    for t in teams:
        base = []
        for k in range(26):
            pid += 1
            pos = POSITIONS[k] if k < 11 else POSITIONS[int(rng.integers(0, 11))]
            players.append([pid, f"{FIRST[int(rng.integers(0, len(FIRST)))]} {LAST[int(rng.integers(0, len(LAST)))]}",
                            date(1990, 1, 1) + timedelta(days=int(rng.integers(0, 365 * 14))),
                            NATION[int(rng.integers(0, len(NATION)))], pos])
            base.append((pid, pos))
        for sid, _, _ in SEASONS:
            squads[(sid, t[0])] = base
    w(f"{a.out}/player.csv", ["player_id", "full_name", "birth_date", "nationality", "primary_position"], players)
    sm = []
    for (sid, tid), lst in squads.items():
        for n, (p, _) in enumerate(lst, start=1):
            sm.append([p, tid, sid, n])
    w(f"{a.out}/squad_membership.csv", ["player_id", "team_id", "cs_id", "shirt_number"], sm)

    # ---- matches -------------------------------------------------------------
    match_rows, mt_rows, pm_rows, shot_rows, ev_rows = [], [], [], [], []
    standings = {}
    mid = shot_id = ev_id = 0
    for sid, _, start in SEASONS:
        fixtures = double_round_robin(N_TEAMS, rng)          # list of (matchday, home, away)
        for md, h, aw in fixtures:
            mid += 1
            ko = datetime.combine(start + timedelta(days=(md - 1) * 7 + int(rng.integers(0, 3))),
                                  datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=int(rng.choice([12, 15, 17, 20])))
            # expected goals: home/away baseline x attack strength x opponent's defensive weakness
            lam_h = max(0.2, 1.40 * (attack[h] / 1.35) * (defence[aw] / 1.35))
            lam_a = max(0.2, 1.10 * (attack[aw] / 1.35) * (defence[h] / 1.35))
            hg, ag = int(rng.poisson(lam_h)), int(rng.poisson(lam_a))
            match_rows.append([mid, sid, md, ko.isoformat(), h + 1, aw + 1, hg, ag, "finished"])

            # team stats
            for tid, is_home, gf in ((h + 1, True, hg), (aw + 1, False, ag)):
                shots_n = int(np.clip(rng.normal(12.5 + gf * 1.5, 3), 3, 30))
                xg = round(float(np.clip(gf * 0.55 + rng.normal(0.7, 0.45), 0.1, 6)), 2)
                mt_rows.append([mid, tid, "t" if is_home else "f", xg, shots_n, round(float(rng.normal(50, 7)), 1)])

                # standings accumulators
                st = standings.setdefault((sid, tid), [0, 0, 0, 0, 0, 0, 0])
                ga = ag if is_home else hg
                st[0] += 1
                if gf > ga: st[1] += 1
                elif gf == ga: st[2] += 1
                else: st[3] += 1
                st[4] += gf; st[5] += ga

            # lineups + player stats + shots
            for tid, gf, tm_idx in ((h + 1, hg, h), (aw + 1, ag, aw)):
                squad = squads[(sid, tid)]
                xi = []
                for pos_wanted, n in (("GK", 1), ("DF", 4), ("MF", 3), ("FW", 3)):
                    pool = [p for p in squad if p[1] == pos_wanted]
                    if len(pool) < n:
                        pool = pool + [p for p in squad if p not in pool]
                    pick = rng.choice(len(pool), size=n, replace=False)
                    xi += [pool[i] for i in pick]
                # goals allocated to attackers by weight
                weights = np.array([{"GK": 0.0, "DF": 0.08, "MF": 0.25, "FW": 0.6}[p[1]] for p in xi])
                weights = weights / weights.sum()
                goal_alloc = rng.multinomial(gf, weights) if gf else np.zeros(len(xi), int)
                assist_alloc = rng.multinomial(max(0, gf - int(rng.integers(0, 2))), weights) if gf else np.zeros(len(xi), int)
                tshots = mt_rows[-2 if tid == h + 1 else -1][4]
                # every goal is also a shot: allocate the goals first, then spread the remaining shots
                sw = weights + 0.01
                shot_alloc = goal_alloc + rng.multinomial(max(tshots - gf, 0), sw / sw.sum())
                for k, (p, pos) in enumerate(xi):
                    mins = int(rng.choice([90, 90, 90, 90, 84, 75, 62]))
                    pm_rows.append([mid, p, tid, "t", mins, pos, int(goal_alloc[k]), int(assist_alloc[k]),
                                    int(shot_alloc[k]), round(float(shot_alloc[k]) * 0.11, 2),
                                    int(rng.integers(15, 75)), int(rng.integers(0, 7))])
                    # shots with goals first
                    g_left = int(goal_alloc[k])
                    for s in range(int(shot_alloc[k])):
                        shot_id += 1
                        is_goal = g_left > 0
                        if is_goal: g_left -= 1
                        shot_rows.append([shot_id, mid, p, tid, int(rng.integers(1, 95)),
                                          round(float(rng.uniform(70, 100)), 1), round(float(rng.uniform(20, 80)), 1),
                                          round(float(np.clip(rng.beta(1.2, 7) + (0.25 if is_goal else 0), 0.01, 0.98)), 3),
                                          "goal" if is_goal else str(rng.choice(["saved", "blocked", "off_target", "post"], p=[0.38, 0.27, 0.32, 0.03])),
                                          str(rng.choice(["right_foot", "left_foot", "head", "other"], p=[0.5, 0.3, 0.18, 0.02]))])
                # events (bulk) -- vectorised for speed
                n_ev = a.events_per_match // 2
                ev_players = np.array([p for p, _ in xi])
                pidx = rng.integers(0, len(ev_players), n_ev)
                types = rng.choice(len(EVENT_TYPES), size=n_ev, p=EVENT_P)
                minutes = rng.integers(0, 95, n_ev)
                seconds = rng.integers(0, 60, n_ev)
                xs = np.round(rng.uniform(0, 100, n_ev), 1)
                ys = np.round(rng.uniform(0, 100, n_ev), 1)
                exs = np.round(np.clip(xs + rng.normal(8, 12, n_ev), 0, 100), 1)
                eys = np.round(np.clip(ys + rng.normal(0, 15, n_ev), 0, 100), 1)
                ok = rng.random(n_ev) < 0.82
                recv = ev_players[rng.integers(0, len(ev_players), n_ev)]
                for j in range(n_ev):
                    ev_id += 1
                    et = EVENT_TYPES[types[j]]
                    is_pass = et == "pass"
                    rcv = int(recv[j]) if is_pass and int(recv[j]) != int(ev_players[pidx[j]]) else ""
                    ev_rows.append([ev_id, mid, tid, int(ev_players[pidx[j]]), int(minutes[j]), int(seconds[j]), et,
                                    float(xs[j]), float(ys[j]),
                                    float(exs[j]) if et in ("pass", "carry") else "",
                                    float(eys[j]) if et in ("pass", "carry") else "",
                                    "t" if ok[j] else "f", rcv])

    w(f"{a.out}/match.csv", ["match_id", "cs_id", "matchday", "kickoff_ts", "home_team_id", "away_team_id",
                              "home_goals", "away_goals", "status"], match_rows)
    w(f"{a.out}/match_team.csv", ["match_id", "team_id", "is_home", "xg", "shots", "possession_pct"], mt_rows)
    w(f"{a.out}/player_match.csv", ["match_id", "player_id", "team_id", "started", "minutes_played", "position",
                                    "goals", "assists", "shots", "xg", "passes", "tackles"], pm_rows)
    w(f"{a.out}/shot.csv", ["shot_id", "match_id", "player_id", "team_id", "minute", "x", "y", "xg", "outcome", "body_part"], shot_rows)
    w(f"{a.out}/event.csv", ["event_id", "match_id", "team_id", "player_id", "minute", "second", "event_type",
                              "x", "y", "end_x", "end_y", "successful", "receiver_id"], ev_rows)

    # starting standings left EMPTY on purpose: Portfolio 3 fills them transactionally.
    # (We also write the expected final table so the load can be cross-checked.)
    w(f"{a.out}/_expected_standings.csv",
      ["cs_id", "team_id", "played", "won", "drawn", "lost", "goals_for", "goals_against", "points"],
      [[s, t, v[0], v[1], v[2], v[3], v[4], v[5], 3 * v[1] + v[2]] for (s, t), v in sorted(standings.items())])

    print(f"matches={len(match_rows)} player_match={len(pm_rows)} shots={len(shot_rows)} events={len(ev_rows)}")


if __name__ == "__main__":
    main()
