"""Cloud and browser runs use the same fixed-arrival acceptance and denominators."""
import math
import statistics


def percentile(values, p):
    values = sorted(values)
    return values[math.ceil(len(values) * p) - 1] if values else None


def summarize(rows, questions, rate, seconds, elapsed, started, peak, stopped=False, finished=False):
    good = [r for r in rows if r["ok"]]
    labeled = [r for r in rows if "expected" in questions[r["question_index"]]]
    labeled_good = [r for r in labeled if r["ok"]]
    correct = sum(bool(r.get("correct")) for r in labeled)
    start, end = seconds * .25, min(elapsed, seconds)
    steady = [r for r in good if start <= r["end_offset_s"] < end]
    samples = [{"second": s, "pending": sum(r["sent"] and r["send_offset_s"] <= s < r["end_offset_s"] for r in rows)}
               for s in range(1, seconds + 1)] if finished else []
    tail = [s for s in samples if s["second"] >= start]
    slope = None
    if len(tail) > 1:
        mx, my = statistics.mean(s["second"] for s in tail), statistics.mean(s["pending"] for s in tail)
        slope = sum((s["second"] - mx) * (s["pending"] - my) for s in tail) / sum((s["second"] - mx)**2 for s in tail)
    latency = [r["elapsed_ms"] for r in good]
    result = {
        "completed": len(rows), "successful": len(good), "failed": len(rows) - len(good),
        "success_rate": len(good) / len(rows) if rows else None,
        "duration_ms": elapsed * 1000, "successful_rps": len(good) / elapsed if elapsed > 0 else 0,
        "completed_rps": len(rows) / elapsed if elapsed > 0 else 0,
        "p50_ms": percentile(latency, .5), "p95_ms": percentile(latency, .95), "p99_ms": percentile(latency, .99),
        "mean_ms": statistics.mean(latency) if latency else None, "min_ms": min(latency) if latency else None,
        "max_ms": max(latency) if latency else None,
        "target_rps": rate, "offered_seconds": seconds, "planned": rate * seconds, "started": started,
        "sent": sum(r["sent"] for r in rows) + started - len(rows), "active": started - len(rows),
        "peak_concurrency": peak, "unsent": rate * seconds - started, "stopped": stopped,
        "steady_valid_rps": len(steady) / (end - start) if end > start else None,
        "http_p50_ms": percentile([r["http_ms"] for r in good], .5),
        "max_send_lag_ms": max((r["send_lag_ms"] for r in rows), default=0),
        "last_quarter_p50_ms": percentile([r["elapsed_ms"] for r in good if r["scheduled_offset_s"] >= seconds*.75], .5),
        "correct": correct, "evaluated": len(labeled), "accuracy": correct / len(labeled) if labeled else None,
        "successful_evaluated": len(labeled_good), "response_accuracy": correct / len(labeled_good) if labeled_good else None,
        "client_rejections": sum(not r["sent"] for r in rows), "backlog_samples": samples,
        "backlog_slope_per_second": slope,
        "api_service_p50_ms": percentile([r["response"]["demo"]["service_ms"] for r in good], .5),
        "model_p50_ms": percentile([r["response"]["metrics"]["engine_wait_and_model_ms"] for r in good], .5),
    }
    result["passes_performance_acceptance"] = bool(
        finished and not stopped and rate == 100 and seconds >= 120 and len(rows) == rate * seconds
        and not result["failed"] and result["p50_ms"] is not None and result["p50_ms"] <= 500
        and result["last_quarter_p50_ms"] is not None and result["last_quarter_p50_ms"] <= 500
        and result["steady_valid_rps"] >= 99 and result["max_send_lag_ms"] <= 100 and slope is not None and slope <= .1)
    return result
