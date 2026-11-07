# Portfolio 3: Transactions and Concurrency

**Business problem.** A finished match's score is sometimes corrected after a video review. One correction must change
**four things together**: the `match` row, **both** teams' rows in `standings` (played/won/drawn/lost/goals/points) and a
row in `result_audit`. If only some of them change, the league table is wrong. If two corrections or a reader run at the
same time, more things can go wrong. This portfolio shows each failure mode and its fix with real concurrent
sessions on PostgreSQL 16.

## Files

| File | Purpose |
|---|---|
| `scripts/00_standings_and_functions.sql` | Builds `standings` from `match`, defines `apply_result()` and the transactional `correct_result()`, and the `standings_drift` view (standings vs a fresh recomputation) |
| `scripts/run_scenarios.py` | Runs six scenarios using several database connections (threads) and prints a timeline |
| `results/scenarios.log` | The saved output of the last run (evidence) |

```bash
psql -d dataxi -f scripts/00_standings_and_functions.sql
python scripts/run_scenarios.py --dsn "host=127.0.0.1 port=5433 dbname=dataxi user=postgres"
```

The script resets the data before each scenario and ends with `standings drift rows = 0`.

## The unit of work: `correct_result(match_id, new_home, new_away, reason)`

Inside one transaction it (1) locks the match row `FOR UPDATE`; (2) locks both standings rows **in ascending `team_id`
order** (important for scenario 5); (3) subtracts the old result from both teams; (4) adds the new result; (5) updates the
match; (6) inserts the audit row.

## Scenario results (from `results/scenarios.log`)

| # | Scenario | What happened | Lesson |
|---|---|---|---|
| 1 | **Atomic commit** (A, C, I, D) | Match 771 changed 2-1 to 2-2. Team 1 went from 37 to 35 points, team 17 from 49 to 50, the audit row was written, and `standings_drift` = 0. Before the commit, a second session still saw the **old** score (2, 1) | all-or-nothing, readers never see half a correction |
| 2 | **Rollback** and **savepoint** | After a correct update, a buggy second statement violated `standings_points_ok`. `ROLLBACK` undid the correct first step too: score back to 2-1, standings unchanged, 0 audit rows. In a batch of three ingestion jobs where the middle one has a bad foreign key, `ROLLBACK TO SAVEPOINT` removed only that job: 2 of 3 stored | atomicity; savepoints give partial rollback inside one transaction |
| 3 | **Isolation levels** | Under **READ COMMITTED** (default) a second read in the same transaction saw team 1's points change from 37 to 35 and the count of ingestion jobs go from 0 to 1 (a non-repeatable read and a phantom). Under **REPEATABLE READ** both reads were identical (37 and 0). A write on a row another transaction had already changed was refused with `could not serialize access due to concurrent update`, so the application must retry. A `READ UNCOMMITTED` request showed **no dirty read**, because PostgreSQL treats it as READ COMMITTED | choose the isolation level per transaction; REPEATABLE READ needs retry logic |
| 4 | **Lost update** | Two workers read the same row, each added one win in the application, and wrote back: **9 wins recorded instead of 10** (no error, silently wrong). Fix 1 `SELECT ... FOR UPDATE` and fix 2 a single `UPDATE ... SET won = won + 1`: both correct (10) | never read-modify-write across statements without a lock; the single atomic statement is simplest |
| 5 | **Deadlock** | T1 locked team A then wanted B; T2 locked B then wanted A. After `deadlock_timeout` (1 s) PostgreSQL aborted T1 with `deadlock detected`; T2 committed. With **both locking in ascending team_id order** both committed (the second simply waited) | a global lock order prevents deadlock; `correct_result()` does this |
| 6 | **Write skew** | Rule: at most 2 running ingestion jobs per provider. One was running; two sessions each counted 1, decided it was safe, and inserted. Under **REPEATABLE READ both committed: 3 running (rule broken)**. Under **SERIALIZABLE** one was aborted with `could not serialize access due to read/write dependencies`; 2 running | snapshot isolation does not protect rules that span rows; SERIALIZABLE (SSI) does, at the price of retries |

## ACID mapping

| Property | Where shown |
|---|---|
| Atomicity | 1, 2 |
| Consistency | `CHECK`/FK constraints (2), `standings_drift` = 0 after every scenario |
| Isolation | 3, 4, 6 |
| Durability | WAL-based commit; shown indirectly in Portfolio 5 (a commit is only durable on the replica if replicated) |

## Notes and limitations

* Scenarios 3 to 6 depend on short `sleep()` calls and thread barriers to force the overlapping order; on a very slow
  machine one of them can occasionally interleave differently. Re-run if a line does not match the table above.
* The row values in the log depend on the match chosen (the first 2-1 result of 2025/26); they will be the same on every
  run with the same seed.
* PostgreSQL differs from the SQL-standard table of anomalies: it has **no** dirty reads at any level and its REPEATABLE
  READ also prevents phantoms (snapshot isolation). The log shows this rather than assuming the textbook.
