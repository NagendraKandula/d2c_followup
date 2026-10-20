"""File reading + validation + import. Rows with problems are NOT evaluated by the rule engine;
they are logged as 'needs data correction' instead."""
import io
import re
from datetime import datetime

import pandas as pd

from .db import FMT, audit, now_str
from .rules import run_rules

COLUMNS = ["order_id", "order_date", "customer_name", "phone", "order_value", "payment_type",
           "order_status", "dispatch_date", "expected_delivery_date", "assigned_employee"]
ALIASES = {
    "orderid": "order_id", "order_no": "order_id", "order_number": "order_id",
    "date": "order_date", "customer": "customer_name", "name": "customer_name",
    "phone_number": "phone", "mobile": "phone", "value": "order_value", "order_amount": "order_value",
    "amount": "order_value", "payment": "payment_type", "status": "order_status",
    "dispatch": "dispatch_date", "dispatched_date": "dispatch_date",
    "expected_delivery": "expected_delivery_date", "edd": "expected_delivery_date",
    "employee": "assigned_employee", "assigned_to": "assigned_employee",
}
STATUS_MAP = {
    "unconfirmed": "Unconfirmed", "pending": "Unconfirmed", "confirmed": "Confirmed",
    "dispatched": "Dispatched", "shipped": "Dispatched", "in transit": "Dispatched",
    "delivered": "Delivered", "failed delivery": "Failed Delivery", "failed": "Failed Delivery",
    "delivery failed": "Failed Delivery", "cancelled": "Cancelled", "canceled": "Cancelled",
}
DATE_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S.%f", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"]
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20000


class UploadError(Exception):
    """Raised for problems with the file as a whole (wrong type, missing columns...)."""


def _norm_col(c):
    c = re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_")
    return ALIASES.get(c, c)


def read_file(filename, content):
    if not filename:
        raise UploadError("No file selected.")
    if len(content) == 0:
        raise UploadError("The uploaded file is empty.")
    if len(content) > MAX_BYTES:
        raise UploadError("File too large (max 5 MB).")
    ext = filename.lower().rsplit(".", 1)[-1]
    try:
        if ext == "csv":
            df = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False, encoding="utf-8-sig")
        elif ext in ("xlsx", "xlsm"):
            df = pd.read_excel(io.BytesIO(content), dtype=str, keep_default_na=False)
        else:
            raise UploadError("Unsupported file type. Please upload a .csv or .xlsx file.")
    except UploadError:
        raise
    except Exception as e:
        raise UploadError(f"Could not read file: {e}")
    df.columns = [_norm_col(c) for c in df.columns]
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise UploadError("Missing required column(s): " + ", ".join(missing))
    if len(df) > MAX_ROWS:
        raise UploadError(f"Too many rows (max {MAX_ROWS}).")
    return df


def _clean(v):
    v = "" if v is None else str(v).strip()
    return "" if v.lower() in ("nan", "nat", "none", "null") else v


def _parse_dt(v):
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(v, f)
        except ValueError:
            continue
    raise ValueError(v)


def validate_row(r, now):
    """Returns (record or None, issues[(type, detail)])."""
    issues = []
    g = lambda k: _clean(r.get(k, ""))
    rec = {}

    for k in ["order_id", "order_date", "customer_name", "phone", "order_value", "payment_type",
              "order_status", "assigned_employee"]:
        if not g(k):
            issues.append(("Missing mandatory field", k))
    rec["order_id"] = g("order_id")
    rec["customer_name"] = g("customer_name")
    rec["assigned_employee"] = g("assigned_employee")
    if rec["order_id"] and not re.fullmatch(r"[A-Za-z0-9_\-/]{1,40}", rec["order_id"]):
        issues.append(("Invalid value", f"order_id '{rec['order_id']}' has invalid characters"))

    # phone
    if g("phone"):
        digits = re.sub(r"\D", "", g("phone"))
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        if len(digits) != 10:
            issues.append(("Invalid value", f"phone '{g('phone')}' is not a 10-digit number"))
        rec["phone"] = digits
    # value
    if g("order_value"):
        try:
            val = float(g("order_value").replace(",", "").replace("₹", ""))
            if val <= 0:
                raise ValueError
            rec["order_value"] = val
        except ValueError:
            issues.append(("Invalid value", f"order_value '{g('order_value')}' must be a positive number"))
    # payment
    if g("payment_type"):
        p = g("payment_type").lower()
        if p in ("prepaid", "cod"):
            rec["payment_type"] = "Prepaid" if p == "prepaid" else "COD"
        else:
            issues.append(("Invalid value", f"payment_type '{g('payment_type')}' must be Prepaid or COD"))
    # status
    status = None
    if g("order_status"):
        status = STATUS_MAP.get(re.sub(r"[\s_]+", " ", g("order_status").lower()))
        if not status:
            issues.append(("Invalid value", f"order_status '{g('order_status')}' not recognised"))
        rec["order_status"] = status

    # dates
    dates = {}
    for k in ["order_date", "dispatch_date", "expected_delivery_date"]:
        if g(k):
            try:
                dates[k] = _parse_dt(g(k))
            except ValueError:
                issues.append(("Invalid date", f"{k} '{g(k)}' is not a valid date"))
    od = dates.get("order_date")
    if od and od > now.replace(microsecond=0) + pd.Timedelta(minutes=5):
        issues.append(("Invalid date", f"order_date '{g('order_date')}' is in the future"))
    if od and "dispatch_date" in dates and dates["dispatch_date"] < od:
        issues.append(("Invalid date", "dispatch_date is earlier than order_date"))
    if od and "expected_delivery_date" in dates and dates["expected_delivery_date"].date() < od.date():
        issues.append(("Invalid date", "expected_delivery_date is earlier than order_date"))

    # stage-dependent mandatory data (otherwise we cannot judge the rules -> data correction, not a flag)
    if status in ("Dispatched", "Delivered", "Failed Delivery") and not g("dispatch_date"):
        issues.append(("Missing mandatory field", f"dispatch_date (required for status {status})"))
    if status in ("Dispatched", "Failed Delivery") and not g("expected_delivery_date"):
        issues.append(("Missing mandatory field", f"expected_delivery_date (required for status {status})"))

    if issues:
        return None, issues
    rec["order_date"] = od.strftime(FMT)
    rec["dispatch_date"] = dates["dispatch_date"].strftime(FMT) if "dispatch_date" in dates else None
    rec["expected_delivery_date"] = (dates["expected_delivery_date"].strftime(FMT)
                                     if "expected_delivery_date" in dates else None)
    return rec, []


def process_upload(conn, filename, content, user, now=None):
    now = now or datetime.now().replace(microsecond=0)
    df = read_file(filename, content)
    cur = conn.execute("INSERT INTO uploads(filename,uploaded_at,uploaded_by) VALUES(?,?,?)",
                       (filename, now_str(now), user))
    upload_id = cur.lastrowid
    seen, issues_out, valid, total = set(), [], [], 0

    for idx, row in df.iterrows():
        r = row.to_dict()
        if all(_clean(v) == "" for v in r.values()):
            continue  # blank line
        total += 1
        rownum = idx + 2
        rec, issues = validate_row(r, now)
        oid = _clean(r.get("order_id"))
        if oid:
            if oid in seen:
                issues = [("Duplicate order ID", f"'{oid}' already appears earlier in this file (first one kept)")] + issues
                rec = None
            seen.add(oid)
        if rec:
            valid.append(rec)
        else:
            for t, d in issues:
                issues_out.append((upload_id, rownum, oid, t, d, now_str(now)))

    new = upd = 0
    for rec in valid:
        exists = conn.execute("SELECT 1 FROM orders WHERE order_id=?", (rec["order_id"],)).fetchone()
        new, upd = (new, upd + 1) if exists else (new + 1, upd)
        conn.execute(
            """INSERT INTO orders(order_id,order_date,customer_name,phone,order_value,payment_type,order_status,
               dispatch_date,expected_delivery_date,assigned_employee,upload_id,updated_at)
               VALUES(:order_id,:order_date,:customer_name,:phone,:order_value,:payment_type,:order_status,
               :dispatch_date,:expected_delivery_date,:assigned_employee,:uid,:ts)
               ON CONFLICT(order_id) DO UPDATE SET order_date=excluded.order_date,
               customer_name=excluded.customer_name, phone=excluded.phone, order_value=excluded.order_value,
               payment_type=excluded.payment_type, order_status=excluded.order_status,
               dispatch_date=excluded.dispatch_date, expected_delivery_date=excluded.expected_delivery_date,
               assigned_employee=excluded.assigned_employee, upload_id=excluded.upload_id,
               updated_at=excluded.updated_at""",
            {**rec, "uid": upload_id, "ts": now_str(now)})
    conn.executemany("INSERT INTO data_issues(upload_id,row_num,order_id,issue_type,detail,created_at) VALUES(?,?,?,?,?,?)",
                     issues_out)
    rejected_rows = len({(i[1]) for i in issues_out})
    result = run_rules(conn, user=user, now=now)
    conn.execute("""UPDATE uploads SET total_rows=?,valid_rows=?,rejected_rows=?,new_orders=?,updated_orders=?,new_tasks=?
                    WHERE id=?""", (total, len(valid), rejected_rows, new, upd, result["created"], upload_id))
    audit(conn, None, None, "File uploaded",
          f"{filename}: {total} rows, {len(valid)} valid, {rejected_rows} need data correction, "
          f"{result['created']} new tasks", user)
    return {"upload_id": upload_id, "filename": filename, "total_rows": total, "valid_rows": len(valid),
            "rejected_rows": rejected_rows, "new_orders": new, "updated_orders": upd,
            "tasks_created": result["created"], "tasks_already_existing": result["already_exist"],
            "tasks_auto_resolved": result["auto_resolved"],
            "issues": [{"row": i[1], "order_id": i[2], "type": i[3], "detail": i[4]} for i in issues_out[:200]]}
