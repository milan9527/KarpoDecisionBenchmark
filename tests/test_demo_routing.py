import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo/backend"))
from routing import ReplicaPool


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_equal_load_rotates_across_all_replicas_like_original_benchmark(self):
        pool = ReplicaPool()
        targets = [("host", i, i) for i in range(6)]
        selected = []
        for _ in range(12):
            target = await pool.acquire("risk", targets)
            selected.append(target[2])
            await pool.release("risk", target)
        self.assertEqual(selected, list(range(6)) * 2)

    async def test_overflow_waits_without_exceeding_model_admission(self):
        pool = ReplicaPool()
        targets = [("host", i, i) for i in range(6)]
        held = [await pool.acquire("risk", targets) for _ in range(96)]
        self.assertEqual(list(pool.busy.values()), [16] * 6)
        waiting = asyncio.create_task(pool.acquire("risk", targets))
        await asyncio.sleep(.01)
        self.assertFalse(waiting.done())
        await pool.release("risk", held[0])
        self.assertEqual(await waiting, held[0])
        self.assertEqual(max(pool.busy.values()), 16)

    async def test_scenarios_are_independent_and_cancellation_leaks_no_slot(self):
        pool = ReplicaPool(limit=1)
        target = [("host", 0, 0)]
        await pool.acquire("risk", target)
        self.assertEqual(await pool.acquire("sop", target), target[0])
        waiting = asyncio.create_task(pool.acquire("risk", target))
        await asyncio.sleep(.01)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        await pool.release("risk", target[0])
        self.assertEqual(await pool.acquire("risk", target), target[0])


if __name__ == "__main__":
    unittest.main()
