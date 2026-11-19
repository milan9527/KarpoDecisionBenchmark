import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

ROOT = pathlib.Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class LifecycleTests(unittest.TestCase):
    def test_stop_never_terminates_for_either_market(self):
        cloud = module("cloud")
        for market in ("spot", "on-demand"):
            with self.subTest(market=market):
                api = Mock()
                api.stop_instances.return_value = {"StoppingInstances": []}
                with patch.object(cloud, "load", return_value={"instance_id": "i-test", "market": market}), \
                        patch.object(cloud, "client", return_value=api), \
                        patch.object(cloud, "save") as save, patch("builtins.print"):
                    cloud.stop()
                api.stop_instances.assert_called_once_with(InstanceIds=["i-test"])
                api.terminate_instances.assert_not_called()
                self.assertIn("stop_requested_utc", save.call_args.args[0])

    def test_unsupported_stop_propagates_without_termination_or_success_record(self):
        cloud = module("cloud")
        api = Mock()
        api.stop_instances.side_effect = ClientError(
            {"Error": {"Code": "UnsupportedOperation", "Message": "One-time Spot cannot stop"}},
            "StopInstances",
        )
        with patch.object(cloud, "load", return_value={"instance_id": "i-test", "market": "spot"}), \
                patch.object(cloud, "client", return_value=api), patch.object(cloud, "save") as save:
            with self.assertRaises(ClientError):
                cloud.stop()
        api.terminate_instances.assert_not_called()
        save.assert_not_called()

    def test_user_pause_blocks_destructive_cleanup_before_any_aws_call(self):
        cleanup = module("cleanup")
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "results").mkdir()
            state = root / "state.json"
            state.write_text(json.dumps({"name": "karpo-benchmark-test"}))
            (root / "results/cloud-lifecycle.json").write_text(
                json.dumps({"cleanup_paused_by_user": True}))
            with patch.object(cleanup, "ROOT", root), patch.object(cleanup, "STATE", state), \
                    patch.object(cleanup.boto3, "client") as client:
                for action in ("terminate", "delete-support"):
                    with self.subTest(action=action), self.assertRaisesRegex(RuntimeError, "stop only"):
                        cleanup.main(action)
                client.assert_not_called()
