"""Single durable cloud benchmark job, independent of the presenter's browser."""
import copy
import gzip
import json
import time

from botocore.exceptions import ClientError

from history import packed
from state import Conflict

ACTIVE = ("preparing", "queued", "running", "archiving")
TERMINAL = ("completed", "stopped", "failed", "interrupted")


class Jobs:
    def __init__(self, table, s3, bucket):
        self.table, self.s3, self.bucket = table, s3, bucket

    def read(self, key):
        item = self.table.get_item(Key={"pk": "cloud-jobs", "sk": key}, ConsistentRead=True).get("Item")
        return json.loads(item["document"]) if item else None

    def change(self, key, transform):
        for _ in range(8):
            old = self.read(key)
            new = transform(copy.deepcopy(old))
            if new is None:
                return old
            args = {"Item": {"pk": "cloud-jobs", "sk": key, "document": packed(new)}}
            if old is None:
                args["ConditionExpression"] = "attribute_not_exists(pk)"
            else:
                args.update(ConditionExpression="#doc = :old", ExpressionAttributeNames={"#doc": "document"},
                            ExpressionAttributeValues={":old": packed(old)})
            try:
                self.table.put_item(**args)
                return new
            except ClientError as error:
                if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
        raise Conflict("云端任务状态正在更新，请重试")

    def heartbeat(self):
        self.change("heartbeat", lambda old: {"at": time.time()})

    def reserve(self, run):
        beat = self.read("heartbeat")
        if not beat or beat["at"] < time.time() - 30:
            raise Conflict("云端压测进程尚未就绪，请稍后重试")

        def take(old):
            if old and old.get("run") == run:
                return None
            if old:
                previous = self.read(old["run"])
                if old.get("expires", 0) > time.time() or previous and previous["state"] in ACTIVE:
                    raise Conflict("已有云端测试正在运行，请等待完成或先停止该轮")
            return {"run": run, "expires": time.time() + 900}
        return self.change("active", take)

    def release(self, run):
        return self.change("active", lambda old: {**old, "expires": 0} if old and old["run"] == run else None)

    def active(self):
        current = self.read("active")
        return self.read(current["run"]) if current else None

    def create(self, run, group, session, settings, definition):
        key = f"history/{run}/cloud-input.json.gz"
        self.s3.put_object(Bucket=self.bucket, Key=key,
                           Body=gzip.compress(packed(definition).encode(), mtime=0),
                           ContentType="application/json", ContentEncoding="gzip", ServerSideEncryption="AES256")
        value = {"id": run, "scenario": group, "session": session, "settings": settings,
                 "state": "queued", "created_at": time.time(), "deadline": time.time() + 600,
                 "input_key": key, "stop_requested": False, "metrics": {}, "error": ""}
        return self.change(run, lambda old: value if not old else None)

    def definition(self, job):
        data = self.s3.get_object(Bucket=self.bucket, Key=job["input_key"])["Body"].read()
        return json.loads(gzip.decompress(data))

    def update(self, run, **changes):
        def apply(old):
            if not old:
                raise KeyError(run)
            old.update(changes)
            return old
        return self.change(run, apply)

    def stop(self, run):
        def request(old):
            if not old:
                raise KeyError(run)
            if old["state"] in ACTIVE:
                old["stop_requested"] = True
            return old
        return self.change(run, request)

    @staticmethod
    def public(job):
        if not job:
            return None
        return {k: v for k, v in job.items() if k not in ("session", "input_key")}


def select_questions(registry, group, body):
    """Select by original IDs; send full original JSON to the model from AWS."""
    dataset = registry.datasets.get(body.get("dataset_id"))
    if not dataset or dataset["group"] != group:
        raise ValueError("请选择该场景的历史问题集")
    rate, seconds = body.get("rate"), body.get("seconds")
    if type(rate) is not int or not 1 <= rate <= 100 or type(seconds) is not int or not 1 <= seconds <= 120:
        raise ValueError("请选择1–100 RPS及1–120秒")
    ids, custom = body.get("question_ids", []), body.get("custom", [])
    if not isinstance(ids, list) or not isinstance(custom, list) or len(custom) > 20 or len(ids) > 2000:
        raise ValueError("问题数量无效")
    lookup = {q["id"]: q for q in dataset["questions"]}
    if any(not isinstance(id_, str) or id_ not in lookup for id_ in ids) or len(set(ids)) != len(ids):
        raise ValueError("历史问题编号无效或重复")
    selected = [copy.deepcopy(lookup[id_]) for id_ in ids]
    for i, question in enumerate(custom):
        if not isinstance(question, dict):
            raise ValueError("自定义问题格式无效")
        raw = question.get("request_json")
        if not isinstance(raw, str) or len(raw.encode()) > 262144:
            raise ValueError("自定义问题过大或格式无效")
        request = json.loads(raw)
        registry.validate(group, request)
        selected.append({"id": f"custom-{i}", "title": str(question.get("title", "自定义问题"))[:200],
                         "request": request, "request_json": raw, "custom": True})
    if not selected:
        raise ValueError("请至少选择一个问题")
    return selected
