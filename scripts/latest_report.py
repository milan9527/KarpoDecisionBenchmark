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
    root = Path(__file__).resolve().parents[1]
    h200 = root / "experiments/vllm-sop-h200-20261120/results/verification.json"
    stopped = root / "operations/stop-20261120/infrastructure-stopped.json"
    promo = root / "experiments/promo-risk-rtx4-20261121/results/verification.json"
    if promo.exists() and json.loads(promo.read_text()).get("verified_requests"):
        from promo_project_report import main as render
    elif h200.exists() and stopped.exists() and json.loads(h200.read_text()).get("confirmed"):
        from project_report import main as render
    elif family_complete():
        from family_report import main as render
    else:
        from concise_report import main as render
    render()
