"""Rule engine: orders -> exceptions (tasks). Idempotent: safe to run any number of times."""
from datetime import datetime, timedelta
from .db import FMT, audit, get_settings, now_str

UNDELIVERED_STATUSES = ("Unconfirmed", "Confirmed", "Dispatched", "Failed Delivery")
CATEGORIES = ["Unconfirmed Order", "Dispatch Delay", "Shipment Delay", "Failed Delivery", "High-Value Order"]


def _dt(s):
    return datetime.strptime(s, FMT) if s else None


def _expected_end(s):
    """A date-only expected delivery date means 'by end of that day'."""
    d = datetime.strptime(s, FMT)
    if d.hour == d.minute == d.second == 0:
        d = d.replace(hour=23, minute=59, second=59)
    return d


def _hours(td):
    return td.total_seconds() / 3600


def evaluate_order(o, now, s):
    """Return list of exception dicts for one order. Skips a rule if its required data is missing."""
    out = []
    status = o["order_status"]
    od = _dt(o["order_date"])

    # Rule 1 - Unconfirmed > N hours
    if status == "Unconfirmed" and od:
        age = _hours(now - od)
        if age > s["unconfirmed_hours"]:
            breach = od + timedelta(hours=s["unconfirmed_hours"])
            out.append(dict(
                rule_code="UNCONFIRMED", category="Unconfirmed Order",
                reason=f"Order unconfirmed for {age:.1f} hours (limit {s['unconfirmed_hours']:g} h)",
                priority="High" if age > 24 else "Medium",
                action="Contact customer for confirmation",
                deadline=breach + timedelta(hours=s["sla_unconfirmed_hours"])))

    # Rule 2 - Confirmed but not dispatched within N hours (no confirmation timestamp in data -> order date used)
    if status == "Confirmed" and od:
        age = _hours(now - od)
        if age > s["dispatch_hours"]:
            breach = od + timedelta(hours=s["dispatch_hours"])
            out.append(dict(
                rule_code="DISPATCH_DELAY", category="Dispatch Delay",
                reason=f"Confirmed order not dispatched after {age:.1f} hours (limit {s['dispatch_hours']:g} h)",
                priority="High" if age > 2 * s["dispatch_hours"] else "Medium",
                action="Ask warehouse to dispatch",
                deadline=breach + timedelta(hours=s["sla_dispatch_hours"])))

    exp = _expected_end(o["expected_delivery_date"]) if o["expected_delivery_date"] else None
    overdue_delivery = exp is not None and now > exp

    # Rule 3 - Dispatched, expected delivery date passed, not delivered
    if status == "Dispatched" and overdue_delivery:
        late = _hours(now - exp)
        out.append(dict(
            rule_code="SHIPMENT_DELAY", category="Shipment Delay",
            reason=f"Expected delivery {o['expected_delivery_date'][:10]} passed {late / 24:.1f} days ago; not delivered",
            priority="High" if late >= 72 else ("Medium" if late >= 24 else "Low"),
            action="Contact courier and investigate",
            deadline=exp + timedelta(hours=s["sla_shipment_hours"])))

    # Rule 4 - Failed delivery attempt (needs immediate follow-up)
    if status == "Failed Delivery":
        out.append(dict(
            rule_code="FAILED_DELIVERY", category="Failed Delivery",
            reason="Delivery attempt failed",
            priority="High", action="Contact customer and arrange another attempt",
            deadline=now + timedelta(hours=s["sla_failed_hours"])))

    # Rule 5 - High value (> threshold), UNDELIVERED, past expected date.
    # "Undelivered" = any live order that has not reached the customer: Unconfirmed, Confirmed,
    # Dispatched or Failed Delivery. Delivered and Cancelled orders never qualify.
    # A missing expected date means the rule cannot be judged -> not flagged (see ingest validation).
    if status in UNDELIVERED_STATUSES and overdue_delivery and o["order_value"] > s["high_value_threshold"]:
        shipped = status in ("Dispatched", "Failed Delivery")
        out.append(dict(
            rule_code="HIGH_VALUE", category="High-Value Order",
            reason=(f"High-value order (Rs {o['order_value']:,.0f} > Rs {s['high_value_threshold']:,.0f}), "
                    f"status {status}, undelivered past expected date {o['expected_delivery_date'][:10]}"),
            priority="High",
            action=("Prioritize customer and courier follow-up" if shipped
                    else "Escalate to warehouse to dispatch immediately and update customer"),
            deadline=exp + timedelta(hours=s["sla_high_value_hours"])))
    return out


def run_rules(conn, user="system", now=None):
    """Create missing tasks, never duplicate; auto-resolve tasks whose condition no longer holds."""
    now = now or datetime.now().replace(microsecond=0)
    s = get_settings(conn)
    desired = {}
    for o in conn.execute("SELECT * FROM orders"):
        for ex in evaluate_order(dict(o), now, s):
            desired[(o["order_id"], ex["rule_code"])] = (dict(o), ex)

    existing = {(r["order_id"], r["rule_code"]): r for r in conn.execute("SELECT * FROM tasks")}
    created = skipped = auto_resolved = 0

    for key, (o, ex) in desired.items():
        if key in existing:
            skipped += 1          # already has a task (open OR resolved) -> do not duplicate
            continue
        cur = conn.execute(
            """INSERT INTO tasks(order_id,rule_code,category,reason,priority,assigned_employee,
               action_required,deadline,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'Open',?,?)""",
            (o["order_id"], ex["rule_code"], ex["category"], ex["reason"], ex["priority"],
             o["assigned_employee"], ex["action"], now_str(ex["deadline"]), now_str(now), now_str(now)))
        audit(conn, o["order_id"], cur.lastrowid, "Task created & assigned",
              f"{ex['category']} | {ex['priority']} | assigned to {o['assigned_employee']}", user)
        created += 1

    for key, t in existing.items():
        if key not in desired and t["status"] != "Resolved":
            conn.execute("UPDATE tasks SET status='Resolved', resolved_at=?, updated_at=? WHERE id=?",
                         (now_str(now), now_str(now), t["id"]))
            audit(conn, t["order_id"], t["id"], "Status changed",
                  f"{t['status']} -> Resolved (auto: condition no longer applies)", user)
            auto_resolved += 1
    return {"created": created, "already_exist": skipped, "auto_resolved": auto_resolved}
