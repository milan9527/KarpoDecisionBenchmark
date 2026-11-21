"""History must not lose failed attempts, duplicate retries or overwrite evidence."""
import base64
import gzip
import io
import json
from pathlib import Path
import sys
import unittest

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo/backend"))
from history import History, packed


def conditional():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")


class Table:
    def __init__(self):
        self.rows = {}

    def get_item(self, Key, **kwargs):
        row = self.rows.get((Key["pk"], Key["sk"]))
        return {"Item": row.copy()} if row else {}

    def put_item(self, Item, ConditionExpression=None, **kwargs):
        key = (Item["pk"], Item["sk"])
        if ConditionExpression == "attribute_not_exists(pk)" and key in self.rows:
            raise conditional()
        if ConditionExpression == "#doc = :old" and self.rows[key]["document"] != kwargs["ExpressionAttributeValues"][":old"]:
            raise conditional()
        self.rows[key] = Item.copy()


class S3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.table, self.s3 = Table(), S3()
        self.history = History(self.table, self.s3, "private")
        self.id = "1795264000000_test_history_0001"
        self.owner = "demo"
        self.history.create({"id": self.id, "scenario": "risk", "kind": "fixed-rps"}, self.owner)
        self.history.all_parts = lambda run, kind=None: (
            json.loads(item["document"]) for (pk, sk), item in self.table.rows.items()
            if pk == run and (not kind or sk.startswith(kind)))

    def part(self, key, rows):
        encoded = base64.b64encode(gzip.compress(packed(rows).encode())).decode()
        return self.history.write_part(self.id, key, encoded, self.owner)

    def test_retries_idempotent_and_conflicting_or_completed_parts_immutable(self):
        rows = [{"index": 1, "ok": False, "status": 429}]
        self.assertEqual(self.part("r000000", rows), self.part("r000000", rows))
        self.assertEqual(len(self.s3.objects), 1)
        with self.assertRaises(ValueError):
            self.part("r000000", [{"index": 1, "ok": True}])
        body = {"status": "completed", "question_count": 0, "metrics": {"completed": 1, "failed": 1}}
        self.assertEqual(self.history.finish(self.id, body, self.owner)["metrics"]["failed"], 1)
        self.history.finish(self.id, body, self.owner)
        self.part("r000000", rows)
        with self.assertRaises(ValueError):
            self.part("r000001", [{"index": 2}])
        self.assertEqual(json.loads(gzip.decompress(self.history.read_part(self.id, "r000000"))), rows)

    def test_cannot_finalize_without_all_results_and_inputs(self):
        self.part("r000000", [{"index": 1}])
        with self.assertRaises(ValueError):
            self.history.finish(self.id, {"status": "stopped", "question_count": 1, "metrics": {"completed": 2}}, self.owner)
        self.part("q000000", [{"id": "q", "request_json": '{"ratio":0.0}'}])
        doc = self.history.finish(self.id, {"status": "stopped", "question_count": 1,
                                          "metrics": {"completed": 1, "unsent": 11999}}, self.owner)
        self.assertEqual(doc["record_count"], 1)
        self.assertEqual(doc["status"], "stopped")

    def test_imports_and_other_accounts_cannot_be_modified(self):
        with self.assertRaises(ValueError):
            self.history.finish(self.id, {"status": "completed"}, "different")
        row = self.table.rows[("runs", self.id)]
        doc = json.loads(row["document"])
        doc["origin"] = "import"
        row["document"] = packed(doc)
        with self.assertRaises(ValueError):
            self.part("r000000", [{"index": 1}])

    def test_interrupted_run_can_resume_and_identifiers_are_bounded(self):
        row = self.table.rows[("runs", self.id)]
        doc = json.loads(row["document"]); doc["updated_epoch"] = 1
        row["document"] = packed(doc)
        self.assertEqual(self.history.get(self.id)["status"], "interrupted")
        self.part("r000000", [{"index": 1, "ok": False}])
        for id_ in ("../outside", "", "x"*200):
            with self.assertRaises(ValueError):
                self.history.get(id_)

    def test_oversized_compressed_input_rejected(self):
        data = base64.b64encode(gzip.compress(b" " * (4*1024*1024+1))).decode()
        with self.assertRaises(ValueError):
            self.history.write_part(self.id, "r000000", data, self.owner)


if __name__ == "__main__":
    unittest.main()
