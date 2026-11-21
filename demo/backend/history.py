"""Durable test history: DynamoDB index and private, immutable S3 evidence."""
import base64
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import re
import time

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError


def packed(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def unpack_item(item):
    return json.loads(item["document"])


class History:
    def __init__(self, table, s3, bucket):
        self.table, self.s3, self.bucket = table, s3, bucket

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Za-z_-]{16,90}", value):
            raise ValueError("无效的历史记录编号")
        return value

    def get(self, run):
        value = self.table.get_item(Key={"pk": "runs", "sk": self.identifier(run)},
                                    ConsistentRead=True).get("Item")
        if not value:
            raise KeyError(run)
        result = unpack_item(value)
        if result["status"] == "running" and time.time() - result["updated_epoch"] > 900:
            result["status"] = "interrupted"
        return result

    def create(self, value, owner):
        run = self.identifier(value["id"])
        if value.get("scenario") not in ("risk", "sop"):
            raise ValueError("未知测试场景")
        if value.get("kind") not in ("fixed-rps", "single"):
            raise ValueError("未知测试类型")
        settings = value.get("settings", {})
        if len(packed(settings)) > 4000:
            raise ValueError("测试设置过大")
        now = datetime.now(timezone.utc).isoformat()
        doc = {"id": run, "title": str(value.get("title", "前端测试"))[:200],
               "scenario": value["scenario"], "kind": value["kind"],
               "dataset_id": str(value.get("dataset_id", "custom"))[:100],
               "started_at": now, "client_started_at": str(value.get("started_at", ""))[:64],
               "updated_epoch": int(time.time()), "status": "running",
               "origin": "demo", "owner": owner, "settings": settings,
               "metrics": {}, "measurement": "浏览器报告；端到端含排队，失败保留",
               "storage": "chunks"}
        try:
            self.table.put_item(Item={"pk": "runs", "sk": run, "document": packed(doc)},
                                ConditionExpression="attribute_not_exists(pk)")
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            old = self.get(run)
            if old.get("owner") != owner or old.get("origin") != "demo":
                raise ValueError("记录编号已存在")
            return old
        return doc

    def listing(self, cursor=None):
        args = {"KeyConditionExpression": Key("pk").eq("runs"), "Limit": 50,
                "ScanIndexForward": False, "ConsistentRead": True}
        if cursor:
            args["ExclusiveStartKey"] = {"pk": "runs", "sk": self.identifier(cursor)}
        result = self.table.query(**args)
        rows = []
        for item in result["Items"]:
            row = unpack_item(item)
            row.pop("owner", None)
            # Keep the list small; full source metrics and links are in detail.
            row.pop("raw_metrics", None)
            row.pop("sources", None)
            if row["status"] == "running" and time.time() - row["updated_epoch"] > 900:
                row["status"] = "interrupted"
            rows.append(row)
        return {"items": rows, "cursor": result.get("LastEvaluatedKey", {}).get("sk")}

    def parts(self, run, kind=None, cursor=None):
        self.identifier(run)
        condition = Key("pk").eq(run)
        if kind:
            if kind not in ("q", "r"):
                raise ValueError("无效记录类型")
            condition &= Key("sk").begins_with(kind)
        args = {"KeyConditionExpression": condition, "Limit": 100, "ConsistentRead": True}
        if cursor:
            if not re.fullmatch(r"[qr][0-9]{6}", cursor):
                raise ValueError("无效分页位置")
            args["ExclusiveStartKey"] = {"pk": run, "sk": cursor}
        result = self.table.query(**args)
        return {"items": [unpack_item(i) for i in result["Items"]],
                "cursor": result.get("LastEvaluatedKey", {}).get("sk")}

    def all_parts(self, run, kind=None):
        cursor = None
        while True:
            page = self.parts(run, kind, cursor)
            yield from page["items"]
            cursor = page["cursor"]
            if not cursor:
                return

    def write_part(self, run, part, encoded, owner):
        doc = self.get(run)
        if doc.get("origin") != "demo" or doc.get("owner") != owner:
            raise ValueError("历史导入记录不可修改")
        if not re.fullmatch(r"[qr][0-9]{6}", part):
            raise ValueError("无效分片编号")
        if not isinstance(encoded, str):
            raise ValueError("缺少压缩分片")
        compressed = base64.b64decode(encoded, validate=True)
        if len(compressed) > 1400000:
            raise ValueError("分片过大")
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("解压后的分片过大")
        rows = json.loads(raw)
        if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
            raise ValueError("分片必须包含1至100条记录")
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("记录格式无效")
        if part[0] == "r" and any(type(r.get("index")) is not int for r in rows):
            raise ValueError("缺少请求序号")
        digest = hashlib.sha256(raw).hexdigest()
        key = f"history/{run}/{part}-{digest}.json.gz"
        marker = {"part": part, "count": len(rows), "sha256": digest, "key": key}
        existing = self.table.get_item(Key={"pk": run, "sk": part}, ConsistentRead=True).get("Item")
        if existing:
            if unpack_item(existing)["sha256"] != digest:
                raise ValueError("已保存的分片不可覆盖")
            return marker
        if doc["status"] not in ("running", "interrupted"):
            raise ValueError("已结束的测试不可追加结果")
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=compressed,
                           ContentType="application/json", ContentEncoding="gzip",
                           ServerSideEncryption="AES256")
        try:
            self.table.put_item(Item={"pk": run, "sk": part, "document": packed(marker)},
                                ConditionExpression="attribute_not_exists(pk)")
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            current = self.table.get_item(Key={"pk": run, "sk": part}, ConsistentRead=True)["Item"]
            if unpack_item(current)["sha256"] != digest:
                raise ValueError("分片发生并发冲突") from None
        return marker

    def finish(self, run, value, owner):
        doc = self.get(run)
        if doc.get("origin") != "demo" or doc.get("owner") != owner:
            raise ValueError("历史导入记录不可修改")
        if value.get("status") not in ("completed", "stopped", "interrupted", "failed"):
            raise ValueError("无效测试状态")
        metrics = value.get("metrics", {})
        if not isinstance(metrics, dict) or len(packed(metrics)) > 60000:
            raise ValueError("指标格式无效或过大")
        parts = list(self.all_parts(run))
        counts = {k: sum(p["count"] for p in parts if p["part"].startswith(k)) for k in ("q", "r")}
        if counts["r"] != metrics.get("completed") or counts["q"] != value.get("question_count"):
            raise ValueError("记录尚未上传完整，请继续同步")
        if doc["status"] not in ("running", "interrupted"):
            if doc["metrics"] != metrics or doc["status"] != value["status"]:
                raise ValueError("已结束的测试不可覆盖")
            return doc
        old = self.table.get_item(Key={"pk": "runs", "sk": run}, ConsistentRead=True)["Item"]["document"]
        doc.update(status=value["status"], metrics=metrics, record_count=counts["r"],
                   question_count=counts["q"], updated_epoch=int(time.time()),
                   finished_at=datetime.now(timezone.utc).isoformat(),
                   error=str(value.get("error", ""))[:1000])
        self.table.put_item(Item={"pk": "runs", "sk": run, "document": packed(doc)},
                            ConditionExpression="#doc = :old",
                            ExpressionAttributeNames={"#doc": "document"},
                            ExpressionAttributeValues={":old": old})
        return doc

    def read_part(self, run, part):
        if not re.fullmatch(r"[qr][0-9]{6}", part):
            raise ValueError("无效分片编号")
        item = self.table.get_item(Key={"pk": self.identifier(run), "sk": part},
                                   ConsistentRead=True).get("Item")
        if not item:
            raise KeyError(part)
        key = unpack_item(item)["key"]
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
