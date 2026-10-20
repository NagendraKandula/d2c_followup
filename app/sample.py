"""Sample data generator (>=100 orders with deliberate exceptions). Dates are relative to 'now'
so the exceptions are always genuine.  Run:  python -m app.sample"""
import os
import random
import sys
from datetime import datetime, timedelta

import pandas as pd

EMPLOYEES = ["Anjali", "Rahul", "Priya", "Arjun", "Meera"]
FIRST = ["Aarav", "Vivaan", "Aditya", "Sai", "Krishna", "Ananya", "Diya", "Isha", "Kavya", "Nisha", "Rohan",
         "Neha", "Suresh", "Lakshmi", "Faisal", "Fathima", "Joseph", "Maria", "Vishnu", "Divya"]
LAST = ["Nair", "Menon", "Pillai", "Kumar", "Reddy", "Raju", "Varma", "Khan", "Thomas", "Das", "Sharma", "Iyer"]
COLS = ["Order ID", "Order Date", "Customer Name", "Phone Number", "Order Value", "Payment Type",
        "Order Status", "Dispatch Date", "Expected Delivery Date", "Assigned Employee"]


def generate(now=None, n_normal=70, start_id=1001, seed=42, with_errors=True):
    now = (now or datetime.now()).replace(microsecond=0)
    rnd = random.Random(seed)
    rows, counter = [], [start_id]

    def ts(dt):
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def day(dt):
        return dt.strftime("%Y-%m-%d")

    def add(status, order_age_h, value=None, disp_after_h=None, exp_in_days=None, pay=None, emp=None):
        oid = f"ORD-{counter[0]}"
        counter[0] += 1
        od = now - timedelta(hours=order_age_h)
        disp = (od + timedelta(hours=disp_after_h)) if disp_after_h is not None else None
        exp = (now + timedelta(days=exp_in_days)) if exp_in_days is not None else None
        rows.append({
            "Order ID": oid, "Order Date": ts(od),
            "Customer Name": f"{rnd.choice(FIRST)} {rnd.choice(LAST)}",
            "Phone Number": str(rnd.choice([9, 8, 7, 6])) + "".join(str(rnd.randint(0, 9)) for _ in range(9)),
            "Order Value": value if value is not None else rnd.choice([499, 799, 999, 1299, 1599, 2199, 2999, 3499, 4499]),
            "Payment Type": pay or rnd.choice(["Prepaid", "COD"]), "Order Status": status,
            "Dispatch Date": ts(disp) if disp else "", "Expected Delivery Date": day(exp) if exp else "",
            "Assigned Employee": emp or rnd.choice(EMPLOYEES)})
        return rows[-1]

    # ---- normal orders ----
    for _ in range(int(n_normal * 0.40)):   # delivered
        a = rnd.uniform(72, 240); add("Delivered", a, disp_after_h=rnd.uniform(4, 20), exp_in_days=-(a / 24 - 4))
    for _ in range(int(n_normal * 0.15)):   # confirmed, within 24h
        add("Confirmed", rnd.uniform(1, 20))
    for _ in range(int(n_normal * 0.10)):   # unconfirmed, within 2h
        add("Unconfirmed", rnd.uniform(0.1, 1.7))
    for _ in range(int(n_normal * 0.25)):   # dispatched, on schedule
        a = rnd.uniform(12, 60); add("Dispatched", a, disp_after_h=rnd.uniform(2, 10), exp_in_days=rnd.uniform(1, 4))
    for _ in range(int(n_normal * 0.10)):
        add("Cancelled", rnd.uniform(5, 100))

    # ---- deliberate exceptions ----
    for _ in range(8):  add("Unconfirmed", rnd.uniform(3, 30))                                   # rule 1
    for _ in range(8):  add("Confirmed", rnd.uniform(26, 90))                                    # rule 2
    for _ in range(8):  # rule 3 (expected date always after the order date)
        a = rnd.uniform(100, 200)
        add("Dispatched", a, disp_after_h=10, exp_in_days=-rnd.uniform(1, a / 24 - 1.5),
            value=rnd.choice([899, 1999, 3499]))
    for _ in range(6):  add("Failed Delivery", rnd.uniform(60, 120), disp_after_h=8,
                           exp_in_days=rnd.choice([-1, 0, 1]), value=rnd.choice([1299, 2499, 3999]))  # rule 4
    for _ in range(6):  # rule 5
        a = rnd.uniform(120, 220)
        add("Dispatched", a, disp_after_h=6, exp_in_days=-rnd.uniform(1, a / 24 - 1.5),
            value=rnd.choice([5500, 7999, 12999, 24999]))
    add("Dispatched", 150, disp_after_h=6, exp_in_days=-2, value=5000)   # boundary: exactly 5000 -> NOT high value
    add("Failed Delivery", 90, disp_after_h=6, exp_in_days=-1, value=9500)  # failed + high value (2 tasks)

    if with_errors:
        base = [dict(r) for r in rows[:5]]
        for b in base[:4]:                       # duplicate order IDs
            rows.append(dict(b))
        a = add("Confirmed", 30); a["Phone Number"] = ""                    # missing phone
        a = add("Confirmed", 30); a["Customer Name"] = ""                   # missing customer
        a = add("Unconfirmed", 5); a["Order Date"] = ""                     # missing order date
        a = add("Unconfirmed", 5); a["Order Date"] = "31/02/2026"           # impossible date
        a = add("Confirmed", 40); a["Order Date"] = "not-a-date"            # garbage date
        a = add("Dispatched", 100, disp_after_h=5, exp_in_days=-2); a["Expected Delivery Date"] = ""  # incomplete shipment
        a = add("Confirmed", 30); a["Assigned Employee"] = ""               # missing employee
        a = add("Confirmed", 30); a["Payment Type"] = "Credit"              # invalid payment type
        a = add("Confirmed", 30); a["Order Value"] = "-250"                 # negative value
        a = add("Dispatched", 100, disp_after_h=5, exp_in_days=-2); a["Dispatch Date"] = ts(now - timedelta(hours=300))  # dispatched before it was ordered
    rnd.shuffle(rows)
    return pd.DataFrame(rows, columns=COLS)


def write_samples(out_dir, now=None):
    os.makedirs(out_dir, exist_ok=True)
    generate(now).to_csv(os.path.join(out_dir, "orders_sample.csv"), index=False)
    generate(now, n_normal=25, start_id=5001, seed=7).to_csv(os.path.join(out_dir, "orders_with_errors.csv"), index=False)


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample_data")
    write_samples(out)
    print("Sample files written to", out)
