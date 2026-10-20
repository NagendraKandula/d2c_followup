# D2C Order Follow-up Automation (FastAPI + SQLite)

Upload an orders file -> rules find problem orders -> each problem becomes a task with owner, priority, action,
deadline -> manager reassigns / updates status -> dashboard, daily CSV report and full audit trail.

## Run (2 minutes)
```bash
python -m venv venv && source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000  -> **Upload** tab -> **Load built-in sample data**. (API docs: /docs)

Data is stored in `data/d2c.db` (SQLite) so tasks and audit records survive refresh/restart.
Sample files: `sample_data/orders_sample.csv` (121 rows) and `orders_with_errors.csv` (for the live interview).
Dates in them are relative to the time they were generated; regenerate fresh ones with `python -m app.sample`
(the "Load built-in sample data" button always generates fresh ones).

## Project layout
| File | Purpose |
|---|---|
| `app/main.py` | FastAPI routes: upload, tasks, dashboard, report, audit, settings |
| `app/ingest.py` | Read CSV/Excel, validate rows (missing fields, duplicate IDs, invalid dates/values), import |
| `app/rules.py` | **Rule engine** (5 rules) + idempotent task generation |
| `app/db.py` | SQLite schema, settings, audit helper |
| `app/sample.py` | Sample data generator (normal + deliberate exceptions) |
| `static/index.html` | UI (dashboard, tasks, upload, data issues, report, audit, rules) |

## How the rules work (`app/rules.py`)
| # | Rule | Condition | Priority | Action | Deadline |
|---|---|---|---|---|---|
| 1 | Unconfirmed | status Unconfirmed and age > 2 h | Medium (High if > 24 h) | Contact customer for confirmation | order+2h + 2h SLA |
| 2 | Dispatch delay | status Confirmed and age > 24 h | Medium (High if > 48 h) | Ask warehouse to dispatch | order+24h + 4h SLA |
| 3 | Shipment delay | Dispatched and expected delivery date passed | Low <1 day late, Medium, High >=3 days | Contact courier and investigate | expected + 24h |
| 4 | Failed delivery | status Failed Delivery | High | Contact customer, arrange another attempt | detection + 4h |
| 5 | High value | value > Rs 5,000, Dispatched/Failed, past expected date | High | Prioritize customer and courier follow-up | expected + 4h |

All thresholds/SLAs are editable in **Rules & settings** (stored in DB; saving re-runs the engine immediately).
Assumption: the dataset has no "confirmed at" timestamp, so rule 2 measures from the order date.
A date-only expected delivery date means "by end of that day".

## Data validation (never silently flagged)
A row is **not evaluated** and is listed under *Data issues* ("needs data correction") when it has: a missing mandatory
field, invalid/impossible date (e.g. 31/02/2026), dispatch or delivery date before the order date, invalid phone / value /
payment type / status, a duplicate order ID in the file (first occurrence kept), or missing dispatch/expected date for a
status that needs it (e.g. Dispatched without expected delivery date).

## Preventing duplicate tasks (live-interview question 4)
Three layers: (1) DB constraint `UNIQUE(order_id, rule_code)` on tasks; (2) orders are **upserted** by order ID, so
re-uploading updates rather than duplicates; (3) the rule engine only inserts tasks whose key does not exist yet
(it reports "already existing" counts). Re-uploading the same file => 0 new tasks (try it).
If an order's condition clears (e.g. status becomes Delivered) its open tasks are auto-resolved and audited.

## Live-interview cheat-sheet
1. **Upload file with errors** -> Upload tab -> "Download a file with errors" -> upload it -> see per-row issues.
2. **Change a rule** -> Rules & settings -> `dispatch_hours` 24 -> 12 -> Save: new Dispatch Delay tasks appear.
   (In code: `dispatch_hours` default in `app/db.py`; logic in `evaluate_order()` in `app/rules.py`.)
3. **Reassign an overdue task** -> Tasks -> tick "Overdue only" -> Reassign. Result: new owner, status reset to Open,
   deadline unchanged unless a new one is entered (so the overdue state stays visible), audit shows
   "Task reassigned: A -> B (task was OVERDUE)". Reassigning resolved tasks is blocked.
4. **Audit** -> Audit trail tab (every create / assignment / status change / reassignment / deadline change /
   rule change / upload, with order ID, timestamp, user).

## Business impact (for the 5-minute demo)
1. **Reduces manual work** - nobody scans Excel/WhatsApp; the rule engine checks every order on every upload and
   hands each employee a ready list (reason, action, deadline).
2. **Prevents forgotten orders** - every exception becomes a tracked task with deadline, overdue highlighting, daily
   report, and an unresolved task can't vanish (it stays until Resolved; audit trail shows who touched it).
3. **Effectiveness metrics** - % orders delivered on time; avg time-to-confirm / time-to-dispatch; exceptions per 100
   orders; avg task resolution time; % tasks resolved before deadline; overdue count trend; failed-delivery-to-redelivery
   rate; RTO/cancellation rate on COD; data-error rate per upload.
4. **Time saved estimate** - (orders/day x minutes of manual checking per order  +  status-chasing messages/day x
   minutes each) - (minutes spent reviewing the dashboard). Example: 300 orders x 0.5 min scan + 40 WhatsApp follow-ups
   x 3 min = 270 min/day manual vs ~45 min with the tool => ~3.75 h/day saved. Validate with a 1-week time study.
5. **Limitations** - no login/roles (user name is typed in); no live courier/WhatsApp/ERP integration; rule 2 uses order
   date (no confirmation timestamp); tasks are created per upload / on "re-run", not on a schedule; a resolved task is
   not re-created if the same problem recurs for the same order; SQLite = single-node; no notifications/escalations.

## Quick API reference
`POST /api/upload` · `POST /api/sample/load` · `GET /api/tasks` · `PATCH /api/tasks/{id}` ·
`GET /api/dashboard` · `GET /api/report/daily?format=csv` · `GET /api/audit` · `GET/PUT /api/settings` · `POST /api/rules/run`
