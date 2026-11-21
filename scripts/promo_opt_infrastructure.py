"""Read-only snapshot of the previously scoped project instances."""
import boto3
import datetime as dt
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
EXP=ROOT/'experiments/promo-opt-20261121'

def main():
    old=json.loads((ROOT/'experiments/promo-risk-rtx4-20261121/operations/final-infrastructure.json').read_text())
    rows=[]
    for region in sorted({r['region'] for r in old['instances']}):
        expected={r['id']:r for r in old['instances'] if r['region']==region}
        result=boto3.client('ec2',region_name=region).describe_instances(InstanceIds=list(expected))
        for reservation in result['Reservations']:
            for instance in reservation['Instances']:
                row=dict(expected[instance['InstanceId']])
                row.update(type=instance['InstanceType'],state=instance['State']['Name'],
                           az=instance['Placement']['AvailabilityZone'],private_ip=instance['PrivateIpAddress'])
                assert row['state']==('running' if row['participates'] else 'stopped'),row
                rows.append(row)
    result={'utc':dt.datetime.now(dt.timezone.utc).isoformat(),'policy':'Read-only verification; EC2 retained by user instruction',
            'instances':sorted(rows,key=lambda r:r['id'])}
    (EXP/'operations/final-infrastructure.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'running':sum(r['state']=='running' for r in rows),'stopped':sum(r['state']=='stopped' for r in rows)}))

if __name__=='__main__':
    main()
