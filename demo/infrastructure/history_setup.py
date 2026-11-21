"""Add only the history table and scoped history permissions; no GPU changes."""
import json
from pathlib import Path
import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]


def main():
    path = ROOT / "operations/demo-20261121/resources.json"
    state = json.loads(path.read_text())
    name = state["name"] + "-history"
    ddb = boto3.client("dynamodb", region_name=state["region"])
    try:
        ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k in ("pk", "sk")])
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceInUseException":
            raise
    # Polling for a small on-demand table typically finishes in seconds.
    ddb.get_waiter("table_exists").wait(TableName=name, WaiterConfig={"Delay": 2, "MaxAttempts": 25})
    arn = ddb.describe_table(TableName=name)["Table"]["TableArn"]
    ddb.update_continuous_backups(TableName=name, PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True})
    iam = boto3.client("iam")
    role = iam.get_instance_profile(InstanceProfileName=state["backend_profile"])["InstanceProfile"]["Roles"][0]["RoleName"]
    iam.put_role_policy(RoleName=role, PolicyName="demo-history", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"], "Resource": arn},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
             "Resource": f"arn:aws:s3:::{state['bucket']}/history/*"}]}))
    state["history_table"] = name
    path.write_text(json.dumps(state, indent=2) + "\n")
    print("History table ready; no GPU instances started.")


if __name__ == "__main__":
    main()
