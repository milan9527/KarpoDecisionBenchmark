"""Select prompt on tune only, then evaluate a frozen fresh synthetic holdout."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import traceback

from promo_opt_controller import collect, launch, run
from promo_opt_runtime import EXP, ROOT, dump


def main():
    manifest_path = EXP / "review-manifest-v1.json"
    manifest = json.loads(manifest_path.read_text())
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    performance = json.loads((EXP / "results/performance-selection.json").read_text())
    best = performance["selected"]
    batch, graph = best["batch"], best["graph"]
    target = EXP / "results/controllers/review.json"
    if target.exists():
        raise RuntimeError("Review phase exists; do not overwrite")
    state = {"status": "running", "completed": [], "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
             "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest()}
    dump(target, state)
    try:
        launch("review-screening", "review-union", batch, graph)
        candidates = []
        for variant in manifest["variants"]:
            name = "review-screen-" + variant
            state["current"] = name
            dump(target, state)
            result = run(name, "review-" + variant, batch, graph, "screen")
            tune = next(row["result"] for row in result["stages"] if row["name"] == "tune")
            responses = [json.loads(line) for line in (
                EXP / "results/runs" / name / "tune/requests.jsonl").read_text().splitlines()]
            reviews = [row for row in responses if row["expected"]["action"] == "review"]
            fixtures = json.loads((EXP / "inputs" / ("review-" + variant) / "risk-requests.json").read_text())
            avg_bytes = sum(len(json.dumps(r["request"], ensure_ascii=False).encode())
                            for r in fixtures if r["group"] == "tune") / tune["attempts"]
            candidates.append({"variant": variant, "tune": tune, "review_attempts": len(reviews),
                               "review_correct": sum(r["correct"] for r in reviews), "mean_json_bytes": avg_bytes})
            state["completed"].append(name)
            dump(target, state)
        eligible = [row for row in candidates if not row["tune"]["failures"]]
        if not eligible:
            raise RuntimeError("All prompt screens have infrastructure failures")
        selected = sorted(eligible, key=lambda row: (-row["tune"]["accuracy"], -row["review_correct"],
                                                     row["mean_json_bytes"], row["variant"]))[0]
        selection = {"selected": selected, "candidates": candidates, "batch": batch, "graph": graph,
                     "basis": "Tune-only selection; no fresh quality predictions have been evaluated",
                     "frozen_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
        dump(EXP / "results/review-selection.json", selection)
        collect()
        run("review-fresh-baseline", "review-baseline", batch, graph, "quality-only")
        variant = selected["variant"]
        run("review-fresh-selected", "review-" + variant, batch, graph, "quality-only")
        # The final deployment uses capture shapes specialized to the selected prompt.
        launch("review-selected", "review-" + variant, batch, graph)
        run("review-confirmed", "review-" + variant, batch, graph, "formal")
        state.update(status="complete", selected=variant)
    except Exception:
        state.update(status="error", error=traceback.format_exc())
        raise
    finally:
        state["ended_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        dump(target, state)
        collect()


if __name__ == "__main__":
    main()
