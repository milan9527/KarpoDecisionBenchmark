"""Describe all business cases used by the latest accepted and legacy runs."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def build():
    scenarios = json.loads((ROOT / "experiments/promo-risk-20261121/scenarios.json").read_text())
    sop = json.loads((ROOT / "fixtures/catalog.json").read_text())
    risk_source = "experiments/promo-rtx6-20261121/inputs/review-baseline/risk-requests.json"
    data = {
        "datasets": [
            {"id": "promo-tune", "group": "risk", "title": "薅羊毛风控 · 正式压力集 · 360题 / 45类",
             "source": risk_source, "split": "tune", "calibrate": True},
            {"id": "promo-quality", "group": "risk", "title": "薅羊毛风控 · 质量集 · 1080题 / 45类",
             "source": risk_source, "split": "quality", "calibrate": True},
            {"id": "resource-risk", "group": "risk", "title": "服务资源风控 · 旧版对照 · 360题 / 6类",
             "source": "experiments/vllm-sop-h200-20261120/risk-requests.json", "calibrate": False},
            {"id": "sop-full", "group": "sop", "title": "SOP完整集 · 104题 / 10个业务域",
             "source": "experiments/vllm-sop-h200-20261120/sop-requests.json"}],
        "risk_titles": {row["id"]: row["description"] for row in scenarios},
        "sop_titles": {row["id"]: row["compact"].split(": ", 1)[1].split(" / ", 1)[0] for row in sop},
        "boundary_titles": {
            "boundary-hold-over-approved": "冻结优先于审批",
            "boundary-hold-over-duplicate": "冻结与重复执行冲突",
            "boundary-hold-over-compromise": "冻结与凭证被盗冲突",
            "boundary-duplicate-over-approved": "防止重复资金操作",
            "boundary-destination-before-execution": "新收款地址未验证",
            "boundary-approval-not-in-customer-text": "用户声称审批但记录缺失",
            "boundary-confirmations-before-deadline": "确认不足且未超时",
            "boundary-confirmation-wait-expired": "确认不足且已超时",
            "boundary-refund-window-boundary": "退款期限最后一天",
            "boundary-refund-one-day-late": "退款超期一天",
            "boundary-limit-equality-approved": "金额恰好等于限额",
            "boundary-domain-injection": "客户消息中的业务域注入"},
    }
    (ROOT / "demo/datasets.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    build()
