"""Durable demo leases shared by the API and an independent stop watchdog."""
import copy
import time
from decimal import Decimal

IDLE_SECONDS = 600
STARTUP_SECONDS = 1800


class Conflict(Exception):
    pass


def fresh(group):
    return {"group": group, "revision": 0, "phase": "stopped", "generation": "",
            "sessions": {}, "requests": {}, "stop_after": 0, "error": "", "hosts": []}


def acquire(value, session, now, generation):
    item = copy.deepcopy(value)
    if item["phase"] == "stopping":
        raise Conflict("GPU正在停止，请待停止完成后重新启动。")
    item["sessions"] = {k: v for k, v in item["sessions"].items() if v > now}
    item["sessions"][session] = now + IDLE_SECONDS
    if item["phase"] in ("stopped", "error"):
        item.update(phase="starting", generation=generation, startup_deadline=now + STARTUP_SECONDS,
                    stop_after=now + STARTUP_SECONDS, error="", commands={}, hosts=[], ready_gpus=0)
    elif item["phase"] == "ending":
        # Ending can occur during cold startup; always reconfirm health on resume.
        item.update(phase="starting", startup_deadline=now + STARTUP_SECONDS,
                    stop_after=now + STARTUP_SECONDS)
    if item["phase"] != "starting":
        item["stop_after"] = max(item["sessions"].values())
    return item


def activity(value, session, now):
    item = copy.deepcopy(value)
    if session not in item["sessions"] or item["sessions"][session] <= now:
        raise Conflict("演示会话已结束或空闲超时，请重新点击启动演示。")
    if item["phase"] not in ("starting", "ready"):
        raise Conflict("当前演示不可续期。")
    item["sessions"][session] = now + IDLE_SECONDS
    if item["phase"] == "ready":
        item["stop_after"] = max(item["sessions"].values())
    return item


def release(value, session, now):
    item = copy.deepcopy(value)
    # Repeated end calls must not postpone an already scheduled shutdown.
    if session not in item["sessions"]:
        return item
    item["sessions"] = {k: v for k, v in item["sessions"].items() if k != session and v > now}
    if item["phase"] in ("starting", "ready", "ending"):
        if item["sessions"]:
            if item["phase"] != "starting":
                item["stop_after"] = max(item["sessions"].values())
        else:
            item.update(phase="ending", stop_after=now + IDLE_SECONDS)
    return item


def begin_request(value, session, now, request_id):
    item = activity(value, session, now)
    if item["phase"] != "ready":
        raise Conflict("模型尚未就绪，请等待所有GPU副本健康检查完成。")
    item["requests"] = {k: v for k, v in item["requests"].items() if v > now}
    if len(item["requests"]) >= 4:
        raise Conflict("演示请求正在处理，请稍后重试。")
    item["requests"][request_id] = now + 150
    return item


def claim_stop(value, now):
    item = copy.deepcopy(value)
    if item["phase"] == "stopped":
        return None
    if item["phase"] == "stopping":
        if item.get("stop_claim_until", 0) > now:
            return None
    elif item["stop_after"] > now or any(v > now for v in item["requests"].values()):
        return None
    item.update(phase="stopping", stop_claim_until=now + 90, sessions={}, requests={})
    return item


def plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == int(value) else float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


class Store:
    def __init__(self, table):
        self.table = table

    def get(self, group):
        return plain(self.table.get_item(Key={"group": group}, ConsistentRead=True).get("Item", fresh(group)))

    def update(self, group, transform):
        from botocore.exceptions import ClientError
        for _ in range(12):
            old = self.get(group)
            new = transform(old)
            if new is None:
                return None
            new = copy.deepcopy(new)
            new["revision"] = old["revision"] + 1
            condition = "attribute_not_exists(revision) OR revision = :revision"
            try:
                self.table.put_item(Item=new, ConditionExpression=condition,
                                    ExpressionAttributeValues={":revision": old["revision"]})
                return new
            except ClientError as error:
                if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                time.sleep(.02)
        raise Conflict("演示状态正在更新，请重试。")
