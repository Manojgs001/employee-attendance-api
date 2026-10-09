# DECISIONS.md

1. **Indexes.**
   - `employees: {emp_code: 1} (unique)`: Guarantees unique employee identifiers across concurrent creations and accelerates lookups.
   - `employees: {department: 1, joined_on: 1}`: Powers department listing, headcount filtering, and analytics lookups.
   - `attendance_logs: {emp_code: 1, date: 1} (unique)`: Guarantees at most one log per employee per attendance day, protecting against punch-in race conditions, and powers natural-key patch lookups.
   - `attendance_logs: {date: -1, emp_code: 1}`: Powers `/attendance` pagination and sorting without in-memory sorting.
   - `attendance_logs: {emp_code: 1, punch_in: -1}`: Powers fast lookup for open punch-outs.
   - `attendance_logs: {date: 1, late_minutes: 1}`: Serves date-range filtering for late leaderboard queries.
   - *Rejected*: `{status: 1, date: 1}` was rejected due to low status cardinality (5 values), which provides poor index selectivity.

2. **Punch-in race.**
   Both requests validate the employee, compute the attendance date, and attempt an `insert_one`. The database unique index on `(emp_code, date)` atomically admits the first write. The second write encounters a duplicate key error (code 11000). The application catches `DuplicateKeyError` and returns HTTP 409, preventing race conditions without check-then-insert locking.

3. **Ties.**
   The leaderboard uses MongoDB's `$rank` window function for standard competition ranking (1, 2, 2, 4), followed by `rank <= limit`. If two employees tie at the cutoff rank, both share that rank and are both returned in the output (exceeding `limit` if necessary), ordered stably by `emp_code` ascending.

4. **Headcount.**
   The department summary aggregation starts from the `employees` collection (filtered by `joined_on <= month_end`), grouping to count active headcount directly. An index-backed `$lookup` correlates each employee's monthly attendance logs. Employees with zero attendance logs retain their headcount contribution while adding zero to attendance metrics.

5. **100x data.**
   For 100x data (~10M logs), I would shard `attendance_logs` by `emp_code` and maintain incremental daily/monthly pre-aggregated rollups using MongoDB change streams or scheduled aggregation pipelines, transforming heavy on-demand aggregations into lightweight point queries.
