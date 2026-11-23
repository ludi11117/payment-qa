import csv
import hashlib
import io
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from flask import Flask, g, render_template, request

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, amount INTEGER NOT NULL, paid INTEGER NOT NULL DEFAULT 0,
    refunded INTEGER NOT NULL DEFAULT 0, CHECK (0 <= refunded AND refunded <= paid AND paid <= amount)
);
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
    kind TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount > 0),
    state TEXT NOT NULL DEFAULT 'pending', request_key TEXT NOT NULL,
    UNIQUE(order_id, request_key)
);
CREATE TABLE IF NOT EXISTS ledger (
    operation_id TEXT PRIMARY KEY REFERENCES operations(id), delta INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    order_id TEXT NOT NULL REFERENCES orders(id), event_id TEXT NOT NULL,
    operation_id TEXT NOT NULL REFERENCES operations(id), outcome TEXT NOT NULL,
    amount INTEGER NOT NULL, decision TEXT NOT NULL, PRIMARY KEY(order_id, event_id)
);
CREATE TABLE IF NOT EXISTS statements (
    order_id TEXT NOT NULL REFERENCES orders(id), digest TEXT NOT NULL,
    rows TEXT NOT NULL, PRIMARY KEY(order_id, digest)
);
"""


def create_app(db_path=None):
    app = Flask(__name__)
    path = Path(db_path or Path(app.instance_path) / "payments.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA)

    @app.before_request
    def connect():
        g.db = sqlite3.connect(path, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")

    @app.teardown_request
    def close(_error):
        g.db.close()

    @app.get("/")
    def home():
        return render_template("index.html")

    @app.post("/api/orders")
    def create():
        order_id, payment_id = uuid4().hex[:12], uuid4().hex[:12]
        with g.db:
            g.db.execute("INSERT INTO orders(id, amount) VALUES (?, 10000)", (order_id,))
            g.db.execute("""INSERT INTO operations(id, order_id, kind, amount, request_key)
                            VALUES (?, ?, 'payment', 10000, 'payment')""", (payment_id, order_id))
        return snapshot(order_id), 201

    @app.get("/api/orders/<order_id>")
    def get_order(order_id):
        with g.db:
            g.db.execute("BEGIN")
            return snapshot(order_id) if order(order_id) else error("unknown_order", 404)

    @app.post("/api/orders/<order_id>/refunds")
    def refund(order_id):
        body = json_object()
        amount = body.get("amount")
        key = request.headers.get("Idempotency-Key", "").strip()
        if type(amount) is not int or not 0 < amount <= 10000 or not key or key == "payment":
            return error("invalid_refund", 400)
        db = g.db
        with db:
            db.execute("BEGIN IMMEDIATE")
            current = order(order_id)
            if current is None:
                return error("unknown_order", 404)
            prior = db.execute("SELECT * FROM operations WHERE order_id = ? AND request_key = ?",
                               (order_id, key)).fetchone()
            if prior:
                return (dict(prior), 200) if prior["amount"] == amount else error("key_conflict", 409)
            reserved = pending(order_id)
            if amount > current["paid"] - current["refunded"] - reserved:
                return error("refund_limit", 409)
            op_id = uuid4().hex[:12]
            db.execute("""INSERT INTO operations(id, order_id, kind, amount, request_key)
                          VALUES (?, ?, 'refund', ?, ?)""", (op_id, order_id, amount, key))
            return dict(operation(order_id, op_id)), 201

    @app.post("/api/orders/<order_id>/callbacks")
    def callback(order_id):
        body = json_object()
        op_id, event_id, outcome, amount = (body.get(k) for k in ("operation_id", "event_id", "outcome", "amount"))
        if (not isinstance(op_id, str) or not isinstance(event_id, str) or not event_id.strip()
                or outcome not in ("succeeded", "failed") or type(amount) is not int):
            return error("invalid_event", 400)
        db = g.db
        with db:
            db.execute("BEGIN IMMEDIATE")
            op = operation(order_id, op_id)
            if op is None:
                return error("unknown_operation", 404)
            prior = db.execute("SELECT * FROM events WHERE order_id = ? AND event_id = ?",
                               (order_id, event_id)).fetchone()
            if prior:
                same = (prior["operation_id"], prior["outcome"], prior["amount"]) == (op_id, outcome, amount)
                return {"decision": "duplicate_event"} if same else error("event_id_conflict", 409)
            if amount != op["amount"]:
                return error("amount_mismatch", 409)
            if op["state"] not in ("pending", outcome):
                return error("terminal_state_conflict", 409)
            decision = "duplicate_operation"
            if op["state"] == "pending":
                decision = "applied"
                db.execute("UPDATE operations SET state = ? WHERE id = ?", (outcome, op_id))
                if outcome == "succeeded":
                    delta = amount if op["kind"] == "payment" else -amount
                    db.execute("INSERT INTO ledger VALUES (?, ?)", (op_id, delta))
                    if delta > 0:
                        db.execute("UPDATE orders SET paid = ? WHERE id = ?", (amount, order_id))
                    else:
                        db.execute("UPDATE orders SET refunded = refunded + ? WHERE id = ?", (amount, order_id))
            db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                       (order_id, event_id, op_id, outcome, amount, decision))
            return {"decision": decision}

    @app.post("/api/orders/<order_id>/statements")
    def reconcile(order_id):
        try:
            rows = parse_statement(request.get_data(as_text=True))
        except (ValueError, csv.Error):
            return error("invalid_statement", 400)
        canonical = json.dumps(rows, sort_keys=True)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with g.db:
            g.db.execute("BEGIN IMMEDIATE")
            current = order(order_id)
            if current is None:
                return error("unknown_order", 404)
            saved = g.db.execute("INSERT OR IGNORE INTO statements VALUES (?, ?, ?)",
                                 (order_id, digest, canonical)).rowcount
            state = snapshot(order_id)
            book = {row["operation_id"]: row["delta"] for row in state["ledger"]}
            by_id = [{"operation_id": ref, "local": book.get(ref), "channel": rows.get(ref),
                      "reason": ("missing_local" if ref not in book else
                                 "missing_channel" if ref not in rows else "amount_mismatch")}
                     for ref in sorted(book.keys() | rows.keys()) if book.get(ref) != rows.get(ref)]
            ledger_net = sum(book.values())
            order_net = current["paid"] - current["refunded"]
            return {"statement": digest[:12], "duplicate": not saved, "differences": by_id,
                    "order_net": order_net, "ledger_net": ledger_net, "channel_net": sum(rows.values()),
                    "projection_mismatch": order_net != ledger_net}

    return app


def json_object():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def error(code, status):
    return {"error": code}, status


def order(order_id):
    return g.db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def operation(order_id, op_id):
    return g.db.execute("SELECT * FROM operations WHERE order_id = ? AND id = ?", (order_id, op_id)).fetchone()


def pending(order_id):
    return g.db.execute("""SELECT COALESCE(SUM(amount), 0) FROM operations
                           WHERE order_id = ? AND kind = 'refund' AND state = 'pending'""",
                        (order_id,)).fetchone()[0]


def snapshot(order_id):
    db = g.db
    current = dict(order(order_id))
    current["pending"] = pending(order_id)
    current["available"] = current["paid"] - current["refunded"] - current["pending"]
    current["state"] = ("pending" if not current["paid"] else
                        "paid" if not current["refunded"] else
                        "refunded" if current["refunded"] == current["paid"] else "partially_refunded")
    return {
        "order": current,
        "operations": [dict(r) for r in db.execute("SELECT * FROM operations WHERE order_id = ? ORDER BY rowid", (order_id,))],
        "ledger": [dict(r) for r in db.execute("""SELECT ledger.* FROM ledger
                    JOIN operations ON operations.id = ledger.operation_id
                    WHERE order_id = ? ORDER BY ledger.rowid""", (order_id,))],
        "events": [dict(r) for r in db.execute("SELECT * FROM events WHERE order_id = ? ORDER BY rowid", (order_id,))],
    }


def parse_statement(source):
    reader = csv.reader(io.StringIO(source.lstrip("\ufeff")), strict=True)
    if next(reader, None) != ["operation_id", "delta"]:
        raise ValueError("headers")
    rows = {}
    for ref, cents in reader:
        amount = int(cents)
        if not ref.strip() or ref in rows or not amount:
            raise ValueError("invalid row")
        rows[ref] = amount
    return rows


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.getenv("PAYMENT_QA_PORT", "5051")))
