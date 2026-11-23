import subprocess
import sys
from pathlib import Path


def test_payment_workflow_load_gate(base_url):
    Path("reports").mkdir(exist_ok=True)
    subprocess.run([
        sys.executable, "-m", "locust", "-f", "locustfile.py", "--headless",
        "--host", base_url, "-u", "4", "-r", "4", "-t", "8s", "--only-summary",
        "--stop-timeout", "3", "--csv", "reports/load", "--html", "reports/load.html",
    ], check=True, timeout=60)
