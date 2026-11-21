"""Bound per-replica work to the existing model servers' admission limit."""
import asyncio


class ReplicaPool:
    def __init__(self, limit=16):
        self.limit = limit
        self.busy = {}
        self.cursor = {}
        self.changed = asyncio.Condition()

    async def acquire(self, group, targets):
        async with self.changed:
            await asyncio.wait_for(self.changed.wait_for(
                lambda: any(self.busy.get((group, t[2]), 0) < self.limit for t in targets)), timeout=30)
            cursor = self.cursor.get(group, 0)
            index = min(range(len(targets)), key=lambda i: (
                self.busy.get((group, targets[i][2]), 0), (i - cursor) % len(targets)))
            target = targets[index]
            self.cursor[group] = (index + 1) % len(targets)
            key = (group, target[2])
            self.busy[key] = self.busy.get(key, 0) + 1
            return target

    async def release(self, group, target):
        async with self.changed:
            self.busy[(group, target[2])] -= 1
            self.changed.notify_all()
