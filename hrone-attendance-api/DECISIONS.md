# Design decisions

1. **Indexes.** A unique index on `employees.emp_code` prevents duplicate employee codes even under parallel creates. A unique compound index on `attendance_logs(emp_code, date)` is the concurrency guard for punch-in. Additional indexes support date-sorted attendance listings, employee/month queries, and late/status filtering. Department/join-date indexing supports headcount and department joins. Indexes are created idempotently at startup.

2. **Identical simultaneous punch-ins.** Both requests may initially observe no record, but only one insert can satisfy the unique `(emp_code, date)` index. MongoDB accepts that insert; the other raises `DuplicateKeyError`, translated to HTTP 409. This avoids relying on a race-prone check-then-insert.

3. **Leaderboard cutoff ties.** The pipeline groups late minutes by employee, sorts by total, and uses `$rank`, so equal totals share a rank and the next rank is skipped. The `rank <= limit` filter runs after ranking, meaning all employees tied at the cutoff rank are returned. A final employee-code sort makes tie ordering deterministic.

4. **Employees with no logs.** Department summary starts from employees whose `joined_on` is on or before the month's end, then looks up their logs. This preserves employees with no matching attendance records and counts them in headcount, while zero-stat fields default to zero. Department and headcount come from the employee collection, not logs.

5. **At 100x data.** I would inspect execution-statistics plans, verify index coverage and selectivity, and benchmark realistic concurrent workloads. I would consider sharding only after profiling identifies a single-node limit, likely choosing a shard key aligned with employee/date access patterns. I would also add load tests, observability, and bounded report windows before increasing infrastructure.
