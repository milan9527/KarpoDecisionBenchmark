"""Coalesce per-session GPU protection; inference never writes on completion."""
import asyncio
import time
import uuid

from state import Conflict, IDLE_SECONDS, activity

RENEW_SECONDS = 5
PROTECTION_SECONDS = 150


def protect_session(value, session, now, guard):
    item = activity(value, session, now)
    if item["phase"] != "ready":
        raise Conflict("模型尚未就绪，请等待所有GPU副本健康检查完成。")
    # Cover requests arriving anywhere in the cached renewal window. Explicit
    # End still sets exactly now+600; a crash cannot end the idle period early.
    item["sessions"][session] = now + IDLE_SECONDS + RENEW_SECONDS
    item["stop_after"] = max(item["sessions"].values())
    item["requests"] = {k: v for k, v in item["requests"].items() if v > now}
    item["requests"][guard] = now + PROTECTION_SECONDS
    return item


class SessionLeases:
    def __init__(self, store, groups, wall=time.time, monotonic=time.monotonic):
        self.store, self.wall, self.monotonic = store, wall, monotonic
        self.locks = {group: asyncio.Lock() for group in groups}
        self.cache = {group: {} for group in groups}
        self.worker = uuid.uuid4().hex

    async def enter(self, group, session):
        async with self.locks[group]:
            cached = self.cache[group].get(session)
            if cached and cached[0] > self.monotonic():
                return cached[1]
            renew_before = self.monotonic() + RENEW_SECONDS
            now = int(self.wall())
            value = await asyncio.to_thread(self.store.update, group, lambda old: protect_session(
                old, session, now, "api-" + self.worker + "-" + session))
            self.cache[group] = {k: v for k, v in self.cache[group].items() if v[0] > self.monotonic()}
            self.cache[group][session] = (renew_before, value)
            return value

    async def control(self, group, transform):
        # A completed End/Start must invalidate all cached admission decisions.
        async with self.locks[group]:
            self.cache[group].clear()
            return await asyncio.to_thread(self.store.update, group, transform)
