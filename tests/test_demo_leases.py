import asyncio
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo/backend"))
from leases import SessionLeases
from state import Conflict, acquire, claim_stop, fresh, release


class MemoryStore:
    def __init__(self):
        self.value = acquire(fresh("risk"), "alice", 1000, "generation")
        self.value["phase"] = "ready"
        self.writes = 0

    def update(self, group, transform):
        self.value = transform(copy.deepcopy(self.value))
        self.writes += 1
        return copy.deepcopy(self.value)


class LeaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = 1000
        self.store = MemoryStore()
        self.leases = SessionLeases(self.store, ["risk"], wall=lambda: self.clock, monotonic=lambda: self.clock)

    async def test_hundred_simultaneous_requests_share_one_durable_update(self):
        values = await asyncio.gather(*(self.leases.enter("risk", "alice") for _ in range(100)))
        self.assertEqual(self.store.writes, 1)
        self.assertEqual(len(self.store.value["requests"]), 1)
        self.assertTrue(all(v["phase"] == "ready" for v in values))

    async def test_renewal_once_per_window_covers_last_request_and_crash(self):
        await self.leases.enter("risk", "alice")
        self.clock = 1004
        await self.leases.enter("risk", "alice")
        self.assertEqual(self.store.writes, 1)
        self.assertGreaterEqual(self.store.value["stop_after"], self.clock+600)
        self.clock = 1005
        await self.leases.enter("risk", "alice")
        self.assertEqual(self.store.writes, 2)
        self.assertEqual(len(self.store.value["requests"]), 1)
        self.assertIsNone(claim_stop(self.store.value, 1609))
        self.assertEqual(claim_stop(self.store.value, 1610)["phase"], "stopping")

    async def test_end_invalidates_cached_admission_without_leaking_gpu(self):
        await self.leases.enter("risk", "alice")
        self.clock = 1001
        await self.leases.control("risk", lambda old: release(old, "alice", self.clock))
        with self.assertRaises(Conflict):
            await self.leases.enter("risk", "alice")
        self.assertEqual(self.store.value["stop_after"], 1601)
        self.assertEqual(claim_stop(self.store.value, 1601)["phase"], "stopping")

    async def test_one_presenter_ending_does_not_end_another(self):
        await self.leases.enter("risk", "alice")
        await self.leases.control("risk", lambda old: acquire(old, "bob", self.clock, "unused"))
        await self.leases.enter("risk", "bob")
        await self.leases.control("risk", lambda old: release(old, "alice", self.clock))
        self.assertEqual((await self.leases.enter("risk", "bob"))["phase"], "ready")
        with self.assertRaises(Conflict):
            await self.leases.enter("risk", "alice")

    async def test_expired_or_missing_session_cannot_create_protection(self):
        with self.assertRaises(Conflict):
            await self.leases.enter("risk", "missing")
        self.clock = 1600
        with self.assertRaises(Conflict):
            await self.leases.enter("risk", "alice")
        self.assertEqual(self.store.writes, 0)


if __name__ == "__main__":
    unittest.main()
