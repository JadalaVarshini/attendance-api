# DECISIONS.md

1. **Indexes.** Unique `(emp_code, date)` gives one record per day and per-employee lookups. `(date desc, emp_code)` serves the default attendance list order. `(date, late_minutes, emp_code)` covers the leaderboard. Employees: unique `emp_code`, plus `(department, joined_on)` for headcount. I rejected a separate `emp_code` index on logs, since the unique one's prefix already does that job.

2. **Punch-in race.** Both requests read the employee, then `insert_one`. The unique index lets exactly one insert succeed. The other gets `DuplicateKeyError`, which becomes 409. There is no check-then-insert.

3. **Ties.** Ranking uses `$rank`, so ties share a rank. I filter `rank <= limit` after ranking, so everyone tied at the cutoff is returned, even if that exceeds `limit`.

4. **Headcount.** The summary starts from `employees` (joined by month end) and `$lookup`s their logs. Employees with no logs get an empty array and count as zeros.

5. **At 100x the data.** I'd stop recomputing monthly figures on read. I'd keep pre-aggregated monthly rollups, updated at write time.
