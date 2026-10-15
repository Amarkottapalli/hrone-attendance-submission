# Starter code review

The starter contains multiple correctness, validation, scale, and concurrency defects. Locations refer to the original `app/main.py`.

1. **`health()`** — It always returns 200 without checking MongoDB. A stopped or unreachable database still appears ready. **Fix:** issue `ping` and return 503 on failure.
2. **`create_employee()`** — The duplicate check is a race: parallel requests can both pass it. There is no database uniqueness guarantee. **Fix:** create a unique `emp_code` index and map `DuplicateKeyError` to 409.
3. **`create_employee()`** — `created_at` is a naive datetime and output is not converted to epoch milliseconds. **Fix:** store UTC-aware BSON datetimes and serialize instants as integer epoch milliseconds.
4. **`EmployeeIn`** — The model omits contract validation for employee-code format, email, field lengths, calendar dates, shift formats, and unequal shift boundaries. **Fix:** use Pydantic field/model validation.
5. **`compute_late_minutes()`** — The helper does not handle IST consistently and relies on the input datetime's timezone. **Fix:** normalize instants and calculate against the IST shift-start instant; preserve the strict >10-minute rule.
6. **`compute_work_hours()`** — Python `round()` uses ties-to-even, not required half-up rounding. **Fix:** use `Decimal` with `ROUND_HALF_UP`.
7. **`compute_overtime()`** — It uses the attendance date as a naive/local datetime, mishandles overnight shifts, and returns overtime even when below 30 minutes. **Fix:** calculate the shift end in IST/UTC and apply the 30-minute threshold.
8. **`list_employees()`** — `skip = page * page_size` skips the first page and shifts every subsequent page. **Fix:** use `(page - 1) * page_size`.
9. **`list_employees()`** — `total` counts the whole collection rather than the filtered department result. **Fix:** count with the same filter as the list query.
10. **`list_employees()`** — Results are not sorted by `emp_code`, and `page`/`page_size` have no bounds. **Fix:** sort deterministically and validate page inputs.
11. **`punch_in()`** — It fails to return 404 for an unknown employee before dereferencing `emp`; timestamps are interpreted in the machine's local timezone and seconds are not reliably normalized. **Fix:** validate the employee, validate epoch milliseconds, normalize to UTC seconds, and derive the attendance day in IST.
12. **`punch_in()`** — It checks for an existing record and then inserts, so concurrent requests can create duplicates. It also accepts unsupported status values. **Fix:** use a unique `(emp_code, date)` index and restrict statuses to presence statuses.
13. **`punch_in()`** — Overnight shifts assign the wrong attendance date; derived calculations do not account for IST and business rules. **Fix:** use the shift-aware attendance-date helper and compute lateness consistently.
14. **`list_attendance()`** — It loads every matching record into Python, sorts and paginates in memory, which is not viable for 100,000 records. **Fix:** use indexed MongoDB filtering, sorting, `skip`, `limit`, and `count_documents`.
15. **`list_attendance()`** — It sorts only by date, leaks inconsistent ordering for same-day records, and doesn't exclude `_id` consistently. **Fix:** sort by date descending then employee code ascending and serialize API records.
16. **Missing endpoints** — Punch-out, regularization, all four analytics routes, and explain were unimplemented. **Fix:** implement them against the OpenAPI contract, with atomic updates, audit history, and aggregation pipelines.

Reviewed as sound in concept: using `emp_code` as the business identifier and keeping the two collections separate. The original code's helper functions were not safe to reuse unchanged.
