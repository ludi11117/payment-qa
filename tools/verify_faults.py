"""Check that the targeted tests reject two plausible implementation mistakes."""
import json
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
FAULTS = [
    ("pending_refunds_ignore_reservations", "reserved = pending(order_id)", "reserved = 0",
     "test_parallel_refunds_reserve_capacity"),
    ("reconcile_only_net_amount", "if book.get(ref) != rows.get(ref)",
     "if book.get(ref) != rows.get(ref) and sum(book.values()) != sum(rows.values())",
     "test_reconciliation_detects_offsetting_errors_and_canonical_reimport"),
]


def evaluate(folder, source, test_name):
    (folder / "app.py").write_text(source, encoding="utf-8")
    result = subprocess.run([
        sys.executable, "-B", "-m", "pytest", "-q", f"tests/test_payments.py::{test_name}",
        "--junitxml=result.xml",
    ], cwd=folder, capture_output=True, text=True, errors="replace", timeout=30)
    suite = ElementTree.parse(folder / "result.xml").find("testsuite")
    assert suite.get("tests") == "1" and suite.get("errors") == "0", result.stdout
    failure = suite.find(".//failure")
    if result.returncode == 0:
        return None
    assert result.returncode == 1 and failure is not None, result.stdout
    message = failure.get("message", "")
    assertion = message.removeprefix("AssertionError: ")
    assert assertion.startswith("assert "), message
    return assertion


def main():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    report = []
    with TemporaryDirectory(prefix="payment-qa-faults-") as scratch:
        for name, old, new, test in FAULTS:
            assert source.count(old) == 1, name
            for variant, version in [("baseline", source), ("changed", source.replace(old, new))]:
                folder = Path(scratch) / name / variant
                (folder / "tests").mkdir(parents=True)
                for filename in ["conftest.py", "test_payments.py"]:
                    shutil.copy(ROOT / "tests" / filename, folder / "tests")
                diagnosis = evaluate(folder, version, test)
                if variant == "baseline":
                    assert diagnosis is None, diagnosis
                    print(f"{name}: baseline PASS")
                else:
                    print(f"{name}: changed {'FAIL (detected)' if diagnosis else 'PASS (missed)'}")
                    report.append({"fault": name, "test": test, "detected": diagnosis is not None,
                                   "assertion": diagnosis})
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports/faults.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"{sum(row['detected'] for row in report)} / {len(report)} faults detected")
    return int(any(not row["detected"] for row in report))


if __name__ == "__main__":
    sys.exit(main())
