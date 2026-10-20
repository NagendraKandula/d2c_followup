"""D2C Order Follow-up Automation - FastAPI backend.  Run: uvicorn app.main:app --reload"""
import csv
import io
import os
import re
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import BASE_DIR, DEFAULT_SETTINGS, FMT, audit, db, get_settings, init_db, now_str
from .ingest import UploadError, process_upload
from .rules import CATEGORIES, run_rules
from .sample import generate

app = FastAPI(title="D2C Order Follow-up Automation")
STATUSES = ["Open", "In Progress", "Resolved"]
PRIORITIES = ["High", "Medium", "Low"]


@app.on_event("startup")
def _startup():
    init_db()


@app.exception_handler(Exception)
async def _unhandled(request, exc):
    return JSONResponse(status_code=500, content={"detail": f"Unexpected server error: {exc}"})


def _now():
    return datetime.now().replace(microsecond=0)


def _user(u):
    u = (u or "").strip()
    if not u or len(u) > 50:
        raise HTTPException(400, "A valid user name (1-50 chars) is required.")
    return u


def _date(v, name):
    if v in (None, ""):
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise HTTPException(400, f"{name} must be YYYY-MM-DD")
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"{name} is not a valid date")
    return v


def _employees(conn):
    return [r[0] for r in conn.execute(
        "SELECT assigned_employee FROM orders UNION SELECT assigned_employee FROM tasks ORDER BY 1")]


def _fetch_tasks(conn, date_from=None, date_to=None, category=None, employee=None, priority=None,
                 status=None, overdue_only=False, order_id=None):
    now = now_str(_now())
    date_from, date_to = _date(date_from, "date_from"), _date(date_to, "date_to")
    if category and category not in CATEGORIES:
        raise HTTPException(400, "Unknown category")
    if priority and priority not in PRIORITIES:
        raise HTTPException(400, "Unknown priority")
    if status and status not in STATUSES + ["Unresolved"]:
        raise HTTPException(400, "Unknown status")
    where, args = ["1=1"], [now]
    if date_from: where.append("date(o.order_date) >= ?"); args.append(date_from)
    if date_to: where.append("date(o.order_date) <= ?"); args.append(date_to)
    if category: where.append("t.category = ?"); args.append(category)
    if employee: where.append("t.assigned_employee = ?"); args.append(employee)
    if priority: where.append("t.priority = ?"); args.append(priority)
    if order_id: where.append("t.order_id LIKE ?"); args.append(f"%{order_id}%")
    if status == "Unresolved": where.append("t.status != 'Resolved'")
    elif status: where.append("t.status = ?"); args.append(status)
    if overdue_only: where.append("t.status != 'Resolved' AND t.deadline < ?"); args.append(now)
    sql = f"""SELECT t.*, o.customer_name, o.phone, o.order_value, o.payment_type, o.order_status,
              o.order_date, o.expected_delivery_date,
              CASE WHEN t.status!='Resolved' AND t.deadline < ? THEN 1 ELSE 0 END AS is_overdue
              FROM tasks t JOIN orders o ON o.order_id=t.order_id WHERE {' AND '.join(where)}
              ORDER BY CASE t.priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, t.deadline"""
    return [dict(r) for r in conn.execute(sql, args)]


FILTERS = dict(date_from=Query(None), date_to=Query(None), category=Query(None),
               employee=Query(None), priority=Query(None))


# ------------------------------------------------------------------ meta / settings
@app.get("/api/meta")
def meta():
    with db() as c:
        return {"employees": _employees(c), "categories": CATEGORIES, "priorities": PRIORITIES, "statuses": STATUSES}


@app.get("/api/settings")
def get_settings_api():
    with db() as c:
        return get_settings(c)


class SettingsIn(BaseModel):
    values: dict
    user: str


@app.put("/api/settings")
def update_settings(body: SettingsIn):
    user = _user(body.user)
    with db() as c:
        changes = []
        old = get_settings(c)
        for k, v in body.values.items():
            if k not in DEFAULT_SETTINGS:
                raise HTTPException(400, f"Unknown setting '{k}'")
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise HTTPException(400, f"Setting '{k}' must be a number")
            if v <= 0 or v > 100000:
                raise HTTPException(400, f"Setting '{k}' must be > 0")
            if v != old[k]:
                c.execute("UPDATE settings SET value=? WHERE key=?", (v, k))
                changes.append(f"{k}: {old[k]:g} -> {v:g}")
        if changes:
            audit(c, None, None, "Rule settings changed", "; ".join(changes), user)
        result = run_rules(c, user=user, now=_now())
        return {"changed": changes, "rerun": result, "settings": get_settings(c)}


@app.post("/api/rules/run")
def rules_run(user: str = Query("system")):
    with db() as c:
        return run_rules(c, user=_user(user), now=_now())


# ------------------------------------------------------------------ upload
@app.post("/api/upload")
async def upload(file: UploadFile = File(...), user: str = Form("manager")):
    user = _user(user)
    content = await file.read()
    try:
        with db() as c:
            return process_upload(c, file.filename, content, user, _now())
    except UploadError as e:
        raise HTTPException(400, str(e))


@app.post("/api/sample/load")
def load_sample(user: str = Query("manager")):
    user = _user(user)
    df = generate(_now())
    with db() as c:
        return process_upload(c, "sample_orders (generated).csv", df.to_csv(index=False).encode(), user, _now())


@app.get("/api/sample/download")
def download_sample(kind: str = "sample"):
    name = "orders_with_errors.csv" if kind == "errors" else "orders_sample.csv"
    from .sample import generate as g
    df = g(_now(), n_normal=25, start_id=5001, seed=7) if kind == "errors" else g(_now())
    return Response(df.to_csv(index=False), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@app.get("/api/uploads")
def uploads():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM uploads ORDER BY id DESC LIMIT 20")]


@app.get("/api/data-issues")
def data_issues(upload_id: Optional[int] = None):
    with db() as c:
        if upload_id is None:
            row = c.execute("SELECT MAX(id) FROM uploads").fetchone()
            upload_id = row[0]
        rows = c.execute("SELECT * FROM data_issues WHERE upload_id=? ORDER BY row_num", (upload_id,))
        return {"upload_id": upload_id, "issues": [dict(r) for r in rows]}


@app.post("/api/admin/reset")
def reset(user: str = Query("manager")):
    _user(user)
    with db() as c:
        for t in ["audit", "tasks", "data_issues", "uploads", "orders"]:
            c.execute(f"DELETE FROM {t}")
    return {"ok": True}


# ------------------------------------------------------------------ tasks
@app.get("/api/tasks")
def list_tasks(date_from=FILTERS["date_from"], date_to=FILTERS["date_to"], category=FILTERS["category"],
               employee=FILTERS["employee"], priority=FILTERS["priority"],
               status: Optional[str] = None, overdue: bool = False, order_id: Optional[str] = None):
    with db() as c:
        return _fetch_tasks(c, date_from, date_to, category, employee, priority, status, overdue, order_id)


class TaskUpdate(BaseModel):
    user: str
    status: Optional[str] = None
    assigned_employee: Optional[str] = None
    new_deadline: Optional[str] = None   # optional, for reassignment of overdue tasks


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: int, body: TaskUpdate):
    user = _user(body.user)
    now = _now()
    with db() as c:
        t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            raise HTTPException(404, "Task not found")
        t = dict(t)
        was_overdue = t["status"] != "Resolved" and t["deadline"] < now_str(now)
        status, emp, deadline = t["status"], t["assigned_employee"], t["deadline"]
        log = []

        if body.assigned_employee and body.assigned_employee != t["assigned_employee"]:
            if t["status"] == "Resolved":
                raise HTTPException(409, "Resolved tasks cannot be reassigned. Re-open the task first.")
            if body.assigned_employee not in _employees(c):
                raise HTTPException(400, "Unknown employee")
            emp = body.assigned_employee
            status = "Open"          # new owner starts fresh
            log.append(("Task reassigned",
                        f"{t['assigned_employee']} -> {emp}" + (" (task was OVERDUE)" if was_overdue else "")
                        + (f"; status reset {t['status']} -> Open" if t["status"] != "Open" else "")))

        if body.new_deadline:
            try:
                nd = datetime.strptime(body.new_deadline.replace("T", " ")[:19] if len(body.new_deadline) > 16
                                       else body.new_deadline.replace("T", " ") + ":00", FMT)
            except ValueError:
                raise HTTPException(400, "new_deadline must be a valid date-time")
            if nd <= now:
                raise HTTPException(400, "new_deadline must be in the future")
            if now_str(nd) != deadline:
                log.append(("Deadline changed", f"{deadline} -> {now_str(nd)}"))
                deadline = now_str(nd)

        if body.status and body.status != status:
            if body.status not in STATUSES:
                raise HTTPException(400, f"Status must be one of {STATUSES}")
            log.append(("Status changed", f"{status} -> {body.status}"))
            status = body.status

        if not log:
            raise HTTPException(400, "Nothing to change.")
        resolved_at = now_str(now) if status == "Resolved" else None
        c.execute("""UPDATE tasks SET status=?, assigned_employee=?, deadline=?, updated_at=?, resolved_at=? WHERE id=?""",
                  (status, emp, deadline, now_str(now), resolved_at, task_id))
        for action, details in log:
            audit(c, t["order_id"], task_id, action, details, user)
        return next(x for x in _fetch_tasks(c, order_id=t["order_id"]) if x["id"] == task_id)


# ------------------------------------------------------------------ audit
@app.get("/api/audit")
def audit_log(order_id: Optional[str] = None, limit: int = Query(300, le=2000)):
    with db() as c:
        if order_id:
            rows = c.execute("SELECT * FROM audit WHERE order_id LIKE ? ORDER BY id DESC LIMIT ?",
                             (f"%{order_id}%", limit))
        else:
            rows = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


# ------------------------------------------------------------------ dashboard
@app.get("/api/dashboard")
def dashboard(date_from=FILTERS["date_from"], date_to=FILTERS["date_to"], category=FILTERS["category"],
              employee=FILTERS["employee"], priority=FILTERS["priority"]):
    with db() as c:
        df, dt = _date(date_from, "date_from"), _date(date_to, "date_to")
        ow, oa = ["1=1"], []
        if df: ow.append("date(order_date) >= ?"); oa.append(df)
        if dt: ow.append("date(order_date) <= ?"); oa.append(dt)
        if employee: ow.append("assigned_employee = ?"); oa.append(employee)
        orders_in_scope = c.execute(f"SELECT COUNT(*) FROM orders WHERE {' AND '.join(ow)}", oa).fetchone()[0]
        total_rows = c.execute("SELECT COALESCE(SUM(total_rows),0) FROM uploads").fetchone()[0]
        issues = c.execute("SELECT COUNT(DISTINCT upload_id||'-'||row_num) FROM data_issues").fetchone()[0]

        tasks = _fetch_tasks(c, df, dt, category, employee, priority)
        unresolved = [t for t in tasks if t["status"] != "Resolved"]
        overdue = [t for t in unresolved if t["is_overdue"]]
        by_cat = {k: {"total": 0, "unresolved": 0} for k in CATEGORIES}
        for t in tasks:
            by_cat[t["category"]]["total"] += 1
            by_cat[t["category"]]["unresolved"] += t["status"] != "Resolved"
        emp = {}
        for t in unresolved:
            e = emp.setdefault(t["assigned_employee"], {"employee": t["assigned_employee"], "pending": 0,
                                                         "open": 0, "in_progress": 0, "overdue": 0, "high": 0})
            e["pending"] += 1
            e["open"] += t["status"] == "Open"
            e["in_progress"] += t["status"] == "In Progress"
            e["overdue"] += bool(t["is_overdue"])
            e["high"] += t["priority"] == "High"
        return {
            "kpis": {
                "orders_in_system": orders_in_scope,
                "rows_uploaded_total": total_rows,
                "rows_needing_data_correction": issues,
                "orders_requiring_attention": len({t["order_id"] for t in unresolved}),
                "open_tasks": sum(t["status"] == "Open" for t in tasks),
                "in_progress_tasks": sum(t["status"] == "In Progress" for t in tasks),
                "overdue_tasks": len(overdue),
                "resolved_tasks": sum(t["status"] == "Resolved" for t in tasks),
            },
            "by_category": by_cat,
            "by_employee": sorted(emp.values(), key=lambda e: -e["pending"]),
            "high_priority": [t for t in unresolved if t["priority"] == "High"][:50],
        }


# ------------------------------------------------------------------ daily report
def _safe(v):
    """Neutralise CSV/Excel formula injection."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


def _build_report(conn, day):
    tasks = _fetch_tasks(conn)
    unresolved = [t for t in tasks if t["status"] != "Resolved"]
    sections = {
        "NEW (created on report date)": [t for t in tasks if t["created_at"][:10] == day],
        "UNRESOLVED": unresolved,
        "OVERDUE": [t for t in unresolved if t["is_overdue"]],
        "HIGH PRIORITY (unresolved)": [t for t in unresolved if t["priority"] == "High"],
    }
    emp = {}
    for t in unresolved:
        e = emp.setdefault(t["assigned_employee"], {"pending": 0, "overdue": 0, "high": 0, "actions": []})
        e["pending"] += 1
        e["overdue"] += bool(t["is_overdue"])
        e["high"] += t["priority"] == "High"
        e["actions"].append(f"{t['order_id']}: {t['action_required']}")
    return sections, emp


@app.get("/api/report/daily")
def daily_report(date: Optional[str] = None, format: str = "json"):
    day = _date(date, "date") or _now().strftime("%Y-%m-%d")
    with db() as c:
        sections, emp = _build_report(c, day)
    if format == "json":
        return {"date": day, "sections": sections,
                "employee_pending": [{"employee": k, **{x: v[x] for x in ("pending", "overdue", "high")},
                                      "actions": v["actions"]} for k, v in sorted(emp.items())]}
    if format != "csv":
        raise HTTPException(400, "format must be json or csv")
    buf = io.StringIO()
    w = csv.writer(buf)
    head = ["Section", "Order ID", "Customer", "Phone", "Order Value", "Category", "Reason", "Priority",
            "Assigned Employee", "Action Required", "Deadline", "Status", "Overdue", "Created At"]
    w.writerow(["Daily Exception Report", day, "Generated", now_str(_now())])
    w.writerow(head)
    for name, items in sections.items():
        for t in items:
            w.writerow([_safe(x) for x in [name, t["order_id"], t["customer_name"], t["phone"], t["order_value"],
                        t["category"], t["reason"], t["priority"], t["assigned_employee"], t["action_required"],
                        t["deadline"], t["status"], "Yes" if t["is_overdue"] else "No", t["created_at"]]])
    w.writerow([])
    w.writerow(["EMPLOYEE-WISE PENDING ACTIONS", "Employee", "Pending", "Overdue", "High Priority", "Actions"])
    for k, v in sorted(emp.items()):
        w.writerow(["EMPLOYEE SUMMARY", _safe(k), v["pending"], v["overdue"], v["high"], _safe(" | ".join(v["actions"]))])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=daily_exception_report_{day}.csv"})


# ------------------------------------------------------------------ frontend
STATIC = os.path.join(BASE_DIR, "static")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
