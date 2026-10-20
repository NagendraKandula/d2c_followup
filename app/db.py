"""SQLite persistence: orders, tasks, audit trail, uploads, data issues, settings."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("D2C_DB", os.path.join(BASE_DIR, "data", "d2c.db"))
FMT = "%Y-%m-%d %H:%M:%S"

# Business rules are configurable (Settings tab) -> change a rule without touching code.
DEFAULT_SETTINGS = {
    "unconfirmed_hours": 2,        # Rule 1: unconfirmed for more than N hours
    "dispatch_hours": 24,          # Rule 2: confirmed but not dispatched within N hours
    "high_value_threshold": 5000,  # Rule 5: order value above N rupees
    "sla_unconfirmed_hours": 2,    # task deadline = breach time + SLA
    "sla_dispatch_hours": 4,
    "sla_shipment_hours": 24,
    "sla_failed_hours": 4,
    "sla_high_value_hours": 4,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
  order_id TEXT PRIMARY KEY, order_date TEXT NOT NULL, customer_name TEXT NOT NULL,
  phone TEXT NOT NULL, order_value REAL NOT NULL, payment_type TEXT NOT NULL,
  order_status TEXT NOT NULL, dispatch_date TEXT, expected_delivery_date TEXT,
  assigned_employee TEXT NOT NULL, upload_id INTEGER, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT NOT NULL REFERENCES orders(order_id),
  rule_code TEXT NOT NULL, category TEXT NOT NULL, reason TEXT NOT NULL,
  priority TEXT NOT NULL CHECK(priority IN ('High','Medium','Low')),
  assigned_employee TEXT NOT NULL, action_required TEXT NOT NULL, deadline TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Open' CHECK(status IN ('Open','In Progress','Resolved')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT,
  UNIQUE(order_id, rule_code)          -- prevents duplicate tasks on re-upload
);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, task_id INTEGER,
  action TEXT NOT NULL, details TEXT, timestamp TEXT NOT NULL, user TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads(
  id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, uploaded_at TEXT, uploaded_by TEXT,
  total_rows INTEGER, valid_rows INTEGER, rejected_rows INTEGER, new_orders INTEGER,
  updated_orders INTEGER, new_tasks INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS data_issues(
  id INTEGER PRIMARY KEY AUTOINCREMENT, upload_id INTEGER, row_num INTEGER, order_id TEXT,
  issue_type TEXT, detail TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_audit_order ON audit(order_id);
"""


def now_str(now=None):
    return (now or datetime.now()).strftime(FMT)


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))


def get_settings(conn):
    s = dict(DEFAULT_SETTINGS)
    for r in conn.execute("SELECT key,value FROM settings"):
        s[r["key"]] = r["value"]
    return s


def audit(conn, order_id, task_id, action, details, user):
    conn.execute(
        "INSERT INTO audit(order_id,task_id,action,details,timestamp,user) VALUES(?,?,?,?,?,?)",
        (order_id, task_id, action, details, now_str(), user),
    )
