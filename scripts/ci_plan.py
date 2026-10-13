"""Keep raw deployment plans (including the alert email) in the private S3 bucket.
Only the immutable reference and review digest are public Actions artifacts.
"""
import argparse
import json
from pathlib import Path
import re
import uuid
from release import config, connect, describe_stack, outputs, stack_name, upload_object_once, canonical, save, read


def transfer(direction, reference, plan):
    c=config('infra/deploy.local.json');client,_=connect(c)
    bucket=outputs(describe_stack(client('cloudformation'),stack_name(c,'bootstrap')))['ArtifactBucket']
    if direction=='upload':
        value=read(plan)
        key=f"releases/{value['release_id']}/plans/{uuid.uuid4().hex}.json"
        upload_object_once(client('s3'),bucket,key,canonical(value))
        save(reference,{'key':key,'review_digest':value['review_digest']})
    else:
        ref=read(reference)
        if not re.fullmatch(r'releases/[a-f0-9]{64}/plans/[a-f0-9]{32}\.json',ref['key']):
            raise ValueError('Invalid private plan reference')
        value=json.loads(client('s3').get_object(Bucket=bucket,Key=ref['key'])['Body'].read())
        if value['review_digest']!=ref['review_digest']: raise ValueError('Plan reference digest mismatch')
        save(plan,value)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('direction',choices=['upload','download']);p.add_argument('--reference',required=True);p.add_argument('--plan',required=True)
    a=p.parse_args();transfer(a.direction,a.reference,a.plan)
