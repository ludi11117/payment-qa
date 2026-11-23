import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest
import requests


def new_order(base_url):
    data = requests.post(base_url + "/api/orders", timeout=5).json()
    return base_url + "/api/orders/" + data["order"]["id"], data["operations"][0]


def event(url, op, event_id, outcome="succeeded", amount=None):
    return requests.post(url + "/callbacks", json={
        "operation_id": op["id"], "event_id": event_id, "outcome": outcome,
        "amount": op["amount"] if amount is None else amount,
    }, timeout=5)


def refund(url, key, amount):
    return requests.post(url + "/refunds", json={"amount": amount},
                         headers={"Idempotency-Key": key}, timeout=5)


def paid_order(base_url):
    url, payment = new_order(base_url)
    assert event(url, payment, "first-payment").json() == {"decision": "applied"}
    return url, payment


def test_delayed_payment_and_rejected_callbacks(base_url):
    url, payment = new_order(base_url)
    assert refund(url, "before-payment", 100).status_code == 409
    assert event(url, payment, "wrong-amount", amount=9999).json()["error"] == "amount_mismatch"
    assert requests.get(url, timeout=5).json()["order"]["state"] == "pending"
    assert event(url, payment, "late-payment").json()["decision"] == "applied"
    assert event(url, payment, "late-payment", "failed").json()["error"] == "event_id_conflict"
    assert event(url, payment, "new-failure", "failed").json()["error"] == "terminal_state_conflict"
    data = requests.get(url, timeout=5).json()
    assert data["order"]["paid"] == 10000
    assert data["ledger"] == [{"operation_id": payment["id"], "delta": 10000}]


@pytest.mark.parametrize("reverse", [False, True])
def test_callback_order_does_not_change_money(base_url, db_path, reverse):
    url, payment = paid_order(base_url)
    first = refund(url, "r1", 3000).json()
    second = refund(url, "r2", 2000).json()
    operations = (second, first) if reverse else (first, second)
    for op in operations:
        op_id = op["id"]
        assert event(url, op, op_id).json()["decision"] == "applied"
        assert event(url, op, op_id).json()["decision"] == "duplicate_event"
        assert event(url, op, op_id + "-repeat").json()["decision"] == "duplicate_operation"
    data = requests.get(url, timeout=5).json()
    assert data["order"]["state"] == "partially_refunded"
    assert data["order"]["refunded"] == 5000
    with closing(sqlite3.connect(db_path)) as db:
        assert dict(db.execute("SELECT operation_id, delta FROM ledger")) == {
            payment["id"]: 10000, first["id"]: -3000, second["id"]: -2000,
        }


def test_parallel_refunds_reserve_capacity(base_url):
    url, _ = paid_order(base_url)
    partial = refund(url, "initial", 5000).json()
    assert event(url, partial, "initial-refund").status_code == 200
    barrier = Barrier(2)

    def competing(key):
        barrier.wait(timeout=5)
        return refund(url, key, 4000)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(competing, ["concurrent-a", "concurrent-b"]))
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert next(r for r in responses if r.status_code == 409).json() == {"error": "refund_limit"}
    winner = next(r.json() for r in responses if r.status_code == 201)
    assert event(url, winner, "winner").status_code == 200
    order = requests.get(url, timeout=5).json()["order"]
    assert (order["refunded"], order["pending"], order["available"]) == (9000, 0, 1000)


def test_failed_refund_releases_hold_and_replay_does_not_allocate(base_url):
    url, _ = paid_order(base_url)
    attempt = refund(url, "attempt", 10000).json()
    assert refund(url, "second", 1).status_code == 409
    assert event(url, attempt, "failure", "failed").status_code == 200
    assert refund(url, "attempt", 10000).json()["id"] == attempt["id"]
    assert refund(url, "attempt", 1).json()["error"] == "key_conflict"
    retry = refund(url, "retry", 10000).json()
    assert event(url, retry, "complete").status_code == 200
    state = requests.get(url, timeout=5).json()
    assert state["order"]["state"] == "refunded"
    assert len(state["ledger"]) == 2


def test_callbacks_racing_on_same_transaction(base_url):
    url, _ = paid_order(base_url)
    op = refund(url, "refund", 100).json()
    gate = Barrier(8)

    def callback(i):
        gate.wait(timeout=5)
        return event(url, op, "evt-" + str(i % 2)).json()["decision"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(callback, range(8)))
    assert sorted(decisions) == ["applied"] + ["duplicate_event"] * 6 + ["duplicate_operation"]
    data = requests.get(url, timeout=5).json()
    assert data["order"]["refunded"] == 100
    assert len(data["ledger"]) == 2


def test_reconciliation_detects_offsetting_errors_and_canonical_reimport(base_url, db_path):
    url, payment = paid_order(base_url)
    op = refund(url, "r1", 3000).json()
    assert event(url, op, "r1").status_code == 200
    lines = [f'{payment["id"]},10100', f'{op["id"]},-3100']

    def submit(items):
        return requests.post(url + "/statements", data="operation_id,delta\n" + "\n".join(items),
                             headers={"Content-Type": "text/csv"}, timeout=5)

    result = submit(lines).json()
    assert result["order_net"] == result["ledger_net"] == result["channel_net"] == 7000
    assert len(result["differences"]) == 2
    assert {d["operation_id"] for d in result["differences"]} == {payment["id"], op["id"]}
    reordered = submit(lines[::-1]).json()
    assert reordered["duplicate"] and reordered["statement"] == result["statement"]
    assert reordered["differences"] == result["differences"]
    assert submit([lines[0], lines[0]]).status_code == 400
    with closing(sqlite3.connect(db_path)) as db:
        assert db.execute("SELECT COUNT(*) FROM statements").fetchone()[0] == 1
        db.execute("UPDATE orders SET refunded = refunded + 1")
        db.commit()
    assert submit(lines).json()["projection_mismatch"] is True


def test_statement_missing_on_each_side(base_url):
    url, payment = paid_order(base_url)
    result = requests.post(url + "/statements", data="operation_id,delta\nchannel-only,10000", timeout=5).json()
    assert {d["operation_id"]: d["reason"] for d in result["differences"]} == {
        payment["id"]: "missing_channel", "channel-only": "missing_local",
    }


def test_database_write_failure_rolls_back_callback_and_allows_retry(base_url, db_path):
    url, _ = paid_order(base_url)
    op = refund(url, "refund-to-retry", 3000).json()
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("""CREATE TRIGGER ledger_unavailable BEFORE INSERT ON ledger
                      BEGIN SELECT RAISE(ABORT, 'simulated ledger failure'); END""")
    first = event(url, op, "retryable-event")
    assert first.status_code == 500
    state = requests.get(url, timeout=5).json()
    assert state["operations"][-1]["state"] == "pending"
    assert state["order"]["refunded"] == 0
    assert state["order"]["pending"] == 3000
    assert len(state["ledger"]) == 1
    assert all(e["event_id"] != "retryable-event" for e in state["events"])
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("DROP TRIGGER ledger_unavailable")
    assert event(url, op, "retryable-event").json() == {"decision": "applied"}
    assert requests.get(url, timeout=5).json()["order"]["refunded"] == 3000
