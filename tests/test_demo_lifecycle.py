"""Lifecycle races and deadlines must not depend on a browser remaining open."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo/backend"))
from state import Capacity, Conflict, Store, acquire, activity, begin_request, claim_stop, fresh, release
from watchdog import stop_group


class MemoryStore:
    def __init__(self, value):
        self.value = value

    def get(self, group):
        return copy.deepcopy(self.value)

    def update(self, group, fn):
        value = fn(copy.deepcopy(self.value))
        if value is not None:
            self.value = value
        return value


class LifecycleTests(unittest.TestCase):
    def ready(self):
        value = acquire(fresh("risk"), "alice", 1000, "g1")
        value.update(phase="ready", stop_after=1600)
        return value

    def test_end_schedules_ten_minutes_and_is_idempotent(self):
        value = release(self.ready(), "alice", 1200)
        self.assertEqual(value["stop_after"], 1800)
        self.assertEqual(value["phase"], "ending")
        self.assertEqual(release(value, "alice", 1500)["stop_after"], 1800)
        self.assertIsNone(claim_stop(value, 1799))
        self.assertEqual(claim_stop(value, 1800)["phase"], "stopping")

    def test_reading_status_cannot_extend_idle_deadline(self):
        value = self.ready()
        self.assertIsNone(claim_stop(value, 1599))
        self.assertEqual(claim_stop(value, 1600)["phase"], "stopping")

    def test_one_presenter_cannot_stop_another_active_presenter(self):
        value = acquire(self.ready(), "bob", 1100, "unused")
        value = activity(value, "bob", 1400)
        value = release(value, "alice", 1500)
        self.assertEqual(value["phase"], "ready")
        self.assertEqual(value["stop_after"], 2000)
        self.assertIsNone(claim_stop(value, 1999))

    def test_start_cannot_race_an_already_claimed_stop(self):
        value = claim_stop(self.ready(), 1600)
        with self.assertRaises(Conflict):
            acquire(value, "alice", 1601, "g2")

    def test_resume_during_cold_start_rechecks_readiness(self):
        value = acquire(fresh("risk"), "alice", 1000, "g1")
        value = release(value, "alice", 1100)
        value = acquire(value, "alice", 1101, "g2")
        self.assertEqual(value["phase"], "starting")
        with self.assertRaises(Conflict):
            begin_request(value, "alice", 1102, "r1")

    def test_inflight_request_delays_stop_but_expired_request_cannot_leak(self):
        value = self.ready()
        value["requests"] = {"r1": 1700}
        self.assertIsNone(claim_stop(value, 1601))
        self.assertEqual(claim_stop(value, 1700)["phase"], "stopping")

    def test_one_hundred_requests_are_allowed_and_the_next_is_rejected(self):
        value = self.ready()
        for i in range(100):
            value = begin_request(value, "alice", 1200, str(i))
        self.assertEqual(len(value["requests"]), 100)
        with self.assertRaises(Capacity):
            begin_request(value, "alice", 1200, "overflow")
        value["requests"].pop("0")
        self.assertEqual(len(begin_request(value, "alice", 1200, "replacement")["requests"]), 100)

    def test_expired_requests_do_not_consume_concurrency_slots(self):
        value = self.ready()
        value["requests"] = {str(i): 1200 for i in range(100)}
        value = begin_request(value, "alice", 1200, "new")
        self.assertEqual(value["requests"], {"new": 1350})

    def test_concurrent_local_writers_preserve_all_request_leases(self):
        from concurrent.futures import ThreadPoolExecutor
        import time
        class Table:
            value = self.ready()
            def get_item(self, **kwargs):
                result = copy.deepcopy(self.value)
                time.sleep(.001)
                return {"Item": result}
            def put_item(self, Item, **kwargs):
                self.value = copy.deepcopy(Item)
        store = Store(Table())
        with ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(lambda i: store.update("risk", lambda old: begin_request(old, "alice", 1200, str(i))), range(100)))
        self.assertEqual(len(store.get("risk")["requests"]), 100)
        self.assertEqual(store.get("risk")["revision"], 100)

    def test_startup_has_a_bounded_cleanup_deadline(self):
        value = acquire(fresh("sop"), "alice", 1000, "g1")
        value = activity(value, "alice", 1100)
        self.assertEqual(value["stop_after"], 2800)
        self.assertEqual(claim_stop(value, 2800)["phase"], "stopping")

    def test_watchdog_only_stops_configured_running_instances(self):
        calls = []
        class EC2:
            def describe_instances(self, InstanceIds):
                calls.append(("describe", InstanceIds))
                return {"Reservations": [{"Instances": [
                    {"InstanceId": "i-demo", "State": {"Name": "running"}},
                    {"InstanceId": "i-already-stopped", "State": {"Name": "stopped"}},
                ]}]}
            def stop_instances(self, InstanceIds):
                calls.append(("stop", InstanceIds))
        config = {"groups": {"risk": {"hosts": [
            {"id": "i-demo", "region": "us-east-2"}, {"id": "i-already-stopped", "region": "us-east-2"}]}}}
        store = MemoryStore(self.ready())
        result = stop_group(store, config, "risk", 1600, lambda *args, **kwargs: EC2())
        self.assertEqual(result["action"], "stop_requested")
        self.assertEqual(calls[-1], ("stop", ["i-demo"]))
        self.assertEqual(store.value["phase"], "stopping")

    def test_watchdog_recovers_a_late_start_after_stopped_state(self):
        stopped = []
        class EC2:
            def describe_instances(self, InstanceIds):
                return {"Reservations": [{"Instances": [
                    {"InstanceId": "i-late", "State": {"Name": "running"}}]}]}
            def stop_instances(self, InstanceIds):
                stopped.extend(InstanceIds)
        config = {"groups": {"risk": {"hosts": [{"id": "i-late", "region": "us-east-2"}]}}}
        store = MemoryStore(fresh("risk"))
        result = stop_group(store, config, "risk", 1600, lambda *args, **kwargs: EC2())
        self.assertEqual(result["action"], "stop_requested")
        self.assertEqual(stopped, ["i-late"])


if __name__ == "__main__":
    unittest.main()
