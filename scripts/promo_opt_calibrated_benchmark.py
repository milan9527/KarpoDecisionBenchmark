"""Original open-loop scheduler with validation of both raw and final decisions."""
import json
import time

from benchmark import validate
from promo_opt_calibration import decide, load_calibration
from vllm_rps_benchmark import Runner as RawRunner


class Runner(RawRunner):
    def __init__(self, client, directory, encodings=None):
        super().__init__(client, directory, encodings)
        self.calibration = load_calibration()

    async def call(self, item, sequence, scheduled):
        begin = time.perf_counter()
        row = {"sequence": sequence, "request_id": item["id"], "request_sha256": item["request_sha256"],
               "expected": item["expected"], "scheduled_offset_s": scheduled - self.begin,
               "send_offset_s": begin - self.begin, "send_lag_ms": max(0, begin - scheduled) * 1000,
               "valid": False, "correct": False, "raw_correct": False}
        end = None
        try:
            response = await self.client.post("/v1/systemone", json=item["request"])
            end = time.perf_counter()
            row.update(http_status=response.status_code, response=response.json())
            response.raise_for_status()
            raw_correct = validate(row["response"], item)
            ref = self.encodings[item["id"]]
            metrics = row["response"]["metrics"]
            assert metrics["tokens_sha256"] == ref["tokens_sha256"]
            assert metrics["marker"] == ref["marker"]
            assert row["response"]["usage"]["input_tokens"] == ref["length"]
            threshold, digest = self.calibration
            expected = {**decide(row["response"]["answers"]["action"], threshold), "calibration_sha256": digest}
            assert row["response"]["decision"] == expected
            row.update(valid=True, raw_correct=raw_correct,
                       correct=expected["action"] == item["expected"]["action"])
        except Exception as error:
            row["error"] = f"{type(error).__name__}: {str(error)[:1500]}"
        end = end if end is not None else time.perf_counter()
        row.update(end_offset_s=end - self.begin, latency_ms=(end - scheduled) * 1000,
                   http_latency_ms=(end - begin) * 1000)
        self.rows.append(row)
        self.raw.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        self.raw.flush()
        return row
