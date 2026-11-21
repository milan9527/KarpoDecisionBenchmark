"""Install versioned demo services via SSM without modifying benchmark evidence."""
import argparse
import base64
import json
import io
from pathlib import Path
import shlex
import sys
import time
import uuid
import zipfile

import boto3

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "demo/backend"))
from state import Store, acquire

OUT = ROOT / "operations/demo-20261121"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["start-risk", "start-sop", "nodes", "backend", "watchdog", "status"])
    args = parser.parse_args()
    resources = json.loads((OUT / "resources.json").read_text())
    config = json.loads((ROOT / "demo/config.json").read_text())
    package = json.loads((OUT / "runtime.json").read_text()) if (OUT / "runtime.json").exists() else None
    if args.mode == "watchdog":
        code = io.BytesIO()
        with zipfile.ZipFile(code, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in ("state.py", "watchdog.py"):
                archive.write(ROOT / "demo/backend" / name, name)
        result = boto3.client("lambda", region_name=config["region"]).update_function_code(
            FunctionName=resources["name"] + "-watchdog", ZipFile=code.getvalue())
        (OUT / "watchdog-code.json").write_text(json.dumps(
            {k: result[k] for k in ("FunctionArn", "CodeSha256", "LastModified")}, indent=2) + "\n")
        print("Watchdog code updated", result["LastModified"])
        return
    if args.mode in ("start-risk", "start-sop"):
        # Arm independent cleanup before a potentially expensive cold boot.
        group = args.mode.removeprefix("start-")
        store = Store(boto3.resource("dynamodb", region_name=config["region"]).Table(resources["table"]))
        store.update(group, lambda old: acquire(old, "installation", int(time.time()), uuid.uuid4().hex))
        hosts = config["groups"][group]["hosts"]
        for region in {h["region"] for h in hosts}:
            ec2 = boto3.client("ec2", region_name=region)
            ids = [h["id"] for h in hosts if h["region"] == region]
            stopped = [h["InstanceId"] for r in ec2.describe_instances(InstanceIds=ids)["Reservations"]
                       for h in r["Instances"] if h["State"]["Name"] == "stopped"]
            if stopped:
                ec2.start_instances(InstanceIds=stopped)
        print(group, "startup requested; independent watchdog is armed")
        return
    if args.mode == "status":
        path = OUT / "install-commands.json"
        records = json.loads(path.read_text()) if path.exists() else []
        for record in records:
            result = boto3.client("ssm", region_name=record["region"]).get_command_invocation(
                CommandId=record["command"], InstanceId=record["instance"])
            print(record["name"], result["Status"], result.get("StandardOutputContent", "")[-1500:],
                  result.get("StandardErrorContent", "")[-1000:])
        return
    boto3.client("s3", region_name=config["region"]).upload_file(
        package["archive"], resources["bucket"], package["key"])
    targets = [(group, host) for group, settings in config["groups"].items() for host in settings["hosts"]]
    if args.mode == "backend":
        targets = [("backend", {"id": resources["backend_instance"], "region": config["region"], "name": "backend"})]
    path = OUT / "install-commands.json"
    records = json.loads(path.read_text()) if path.exists() else []
    for group, host in targets:
        ssm = boto3.client("ssm", region_name=host["region"])
        online = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [host["id"]]}])["InstanceInformationList"]
        if not online or online[0]["PingStatus"] != "Online":
            print(host["name"], "SSM not online yet")
            continue
        python = "/opt/karpo-demo/venv/bin/python" if group == "backend" else "/opt/karpo/venv-vllm034/bin/python"
        setup = f"""set -eu
mkdir -p /opt/karpo-demo
{python} - <<'PYCODE'
import boto3,hashlib,tarfile
from pathlib import Path
root=Path('/opt/karpo-demo')
target=root/'runtime.tar.gz'
boto3.client('s3',region_name={config['region']!r}).download_file({resources['bucket']!r},{package['key']!r},str(target))
assert hashlib.sha256(target.read_bytes()).hexdigest()=={package['sha256']!r}
with tarfile.open(target) as archive:
    archive.extractall(root,filter='data')
PYCODE
"""
        if group == "backend":
            unit = """[Unit]
Description=Karpo demo API and GPU startup controller
After=network-online.target
[Service]
WorkingDirectory=/opt/karpo-demo/demo/backend
ExecStart=/opt/karpo-demo/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000 --no-access-log --timeout-keep-alive 210
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5
TimeoutStopSec=30
[Install]
WantedBy=multi-user.target
"""
            setup += f"printf %s {shlex.quote(json.dumps(resources))} > /opt/karpo-demo/resources.json\n"
            setup += f"printf %s {shlex.quote(unit)} > /etc/systemd/system/karpo-demo-api.service\n"
            setup += "systemctl daemon-reload\nsystemctl enable karpo-demo-api\nsystemctl restart karpo-demo-api\n"
            setup += "systemctl is-active karpo-demo-api\n"
        else:
            node = {"group": group, "replicas": host["replicas"], "secret": resources["node_secret"]}
            unit = """[Unit]
Description=Karpo demo GPU gateway and model replicas
After=network-online.target
[Service]
WorkingDirectory=/opt/karpo-demo
ExecStart=/opt/karpo/venv-vllm034/bin/python /opt/karpo-demo/demo/backend/node.py
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=10
TimeoutStopSec=75
KillMode=control-group
[Install]
WantedBy=multi-user.target
"""
            setup += f"printf %s {shlex.quote(json.dumps(node))} > /opt/karpo-demo/node.json\n"
            setup += f"printf %s {shlex.quote(unit)} > /etc/systemd/system/karpo-demo-node.service\n"
            setup += "systemctl daemon-reload\nsystemctl enable karpo-demo-node\n"
            setup += "printf 'GPU demo service installed; model start is controlled by the demo API\\n'\n"
        command = ssm.send_command(InstanceIds=[host["id"]], DocumentName="AWS-RunShellScript",
            Parameters={"commands": [setup], "executionTimeout": ["180"]}, TimeoutSeconds=210)["Command"]["CommandId"]
        records.append({"name": host["name"], "group": group, "region": host["region"],
                        "instance": host["id"], "command": command, "runtime_sha256": package["sha256"]})
        path.write_text(json.dumps(records, indent=2) + "\n")
        print("Install submitted", host["name"], command)


if __name__ == "__main__":
    main()
