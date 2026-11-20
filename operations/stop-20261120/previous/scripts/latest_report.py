"""Select the newest completed, locally available experiment for the main report."""
import json
from pathlib import Path


def family_complete():
    root = Path(__file__).resolve().parents[1]
    status = root / "experiments/family-20261120/results/suite-status.json"
    retry = root / "experiments/qwen-retry-20261120/results/suite-status.json"
    if not status.exists():
        return False
    state = json.loads(status.read_text())["status"]
    return state == "complete" or (
        state == "partial" and retry.exists() and json.loads(retry.read_text())["status"] == "complete")


def main():
    if family_complete():
        from family_report import main as render
    else:
        from concise_report import main as render
    render()
