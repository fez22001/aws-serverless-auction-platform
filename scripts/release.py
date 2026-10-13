"""Review-first release tool. Only build/verify are offline; all other commands access AWS.

No credentials in files. Use a named short-lived profile locally, or GitHub OIDC in CI.
Plans are never executed automatically. Bootstrap always uses the operator's identity.
"""
import argparse
import copy
import hashlib
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from package_lambdas import package
from build_infra import bootstrap, application


def canonical(value): return json.dumps(value,sort_keys=True,separators=(',',':'),default=str).encode()
def digest(value): return hashlib.sha256(canonical(value)).hexdigest()
def read(path): return json.loads(Path(path).read_text())
def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,indent=2,default=str)+'\n')

def build(output=None):
    output=Path(output or ROOT/'.local/releases');output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as folder:
        folder=Path(folder)
        package(folder)
        for name,template in [('bootstrap',bootstrap()),('application',application())]:
            save(folder/(name+'.json'),template)
        files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())}
        release_id=digest(files); target=output/release_id
        save(folder/'manifest.json',{'schema':1,'release_id':release_id,'files':files})
        if target.exists(): verify(target)
        else: shutil.copytree(folder,target)
    return target


def verify(folder):
    folder=Path(folder);manifest=read(folder/'manifest.json')
    if manifest.get('schema')!=1 or digest(manifest['files'])!=manifest['release_id']:
        raise ValueError('Invalid release manifest')
    required={p.name+'.zip' for p in (ROOT/'hw10/lambdas').iterdir() if (p/'lambda_function.py').exists()} | {'application.json','bootstrap.json'}
    if set(manifest['files'])!=required: raise ValueError('Release is missing expected files or contains unexpected files')
    for name,sha in manifest['files'].items():
        if Path(name).name!=name or hashlib.sha256((folder/name).read_bytes()).hexdigest()!=sha:
            raise ValueError(f'Release hash mismatch: {name}')
    return manifest


def config(path):
    c=read(path)
    if not re.fullmatch(r'\d{12}',c.get('expected_account_id','')): raise ValueError('Set expected_account_id to the NEW account ID')
    if c.get('region')!='us-east-1': raise ValueError('This recovery release is reviewed for us-east-1; review templates before changing region')
    if not re.fullmatch(r'[a-z][a-z0-9-]{2,24}',c.get('project_prefix','')): raise ValueError('Invalid project_prefix')
    net=ipaddress.ip_network(c.get('allowed_api_cidr',''),strict=True)
    if str(net) != '0.0.0.0/0' and (net.version!=4 or net.prefixlen!=32 or not net.network_address.is_global):
        raise ValueError('allowed_api_cidr must be 0.0.0.0/0 for authenticated access or an actual public IPv4 /32')
    for flag in ['point_in_time_recovery','manage_api_gateway_logging']:
        if not isinstance(c.get(flag),bool): raise ValueError(f'{flag} must be a JSON boolean')
    if not isinstance(c.get('reserve_lambda_concurrency',True),bool): raise ValueError('reserve_lambda_concurrency must be a JSON boolean')
    budget=c.get('monthly_budget_usd',10)
    if isinstance(budget,bool) or not isinstance(budget,(int,float)) or not 1<=budget<=100000: raise ValueError('monthly_budget_usd must be between 1 and 100000')
    if c.get('github_repository') and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',c['github_repository']): raise ValueError('GitHub repository must be owner/repo')
    if c.get('github_repository'):
        match=re.fullmatch(r'repo:([A-Za-z0-9_.-]+)(?:@[0-9]+)?/([A-Za-z0-9_.-]+)(?:@[0-9]+)?',c.get('github_subject_prefix',''))
        if not match or '/'.join(match.groups()) != c['github_repository']:
            raise ValueError('Set github_subject_prefix to the exact repository OIDC prefix from GitHub')
    return c


def connect(c):
    import boto3
    from botocore.config import Config
    # Refuse endpoint overrides so identity verification and deployment use AWS endpoints.
    if any(k.startswith('AWS_ENDPOINT_URL') for k in os.environ): raise ValueError('Remove AWS_ENDPOINT_URL overrides')
    profile=c.get('profile')
    if not profile and os.environ.get('GITHUB_ACTIONS')!='true': raise ValueError('A named AWS profile is required outside GitHub Actions')
    session=boto3.Session(profile_name=profile or None,region_name=c['region'])
    client_config=Config(connect_timeout=10,read_timeout=35,retries={'max_attempts':3,'mode':'standard'})
    def client(service): return session.client(service,config=client_config)
    identity=client('sts').get_caller_identity()
    if identity['Account']!=c['expected_account_id']: raise ValueError('AWS account mismatch; no resource changes performed')
    return client,identity


def stack_name(c,kind): return c['project_prefix']+'-'+kind

def describe_stack(cf,name):
    from botocore.exceptions import ClientError
    try: return cf.describe_stacks(StackName=name)['Stacks'][0]
    except ClientError as e:
        if e.response['Error']['Code']=='ValidationError' and 'does not exist' in e.response['Error']['Message']: return None
        raise

def outputs(stack): return {o['OutputKey']:o['OutputValue'] for o in stack.get('Outputs',[])}

def check_logging(client,c,kind):
    existing=client('apigateway').get_account().get('cloudwatchRoleArn')
    if kind=='app' and not existing: raise ValueError('API Gateway regional logging role is absent. Configure it through the reviewed bootstrap or explicitly reuse an existing role.')
    if kind=='bootstrap' and not c['manage_api_gateway_logging'] and existing:
        previous=describe_stack(client('cloudformation'),stack_name(c,'bootstrap'))
        if previous:
            owned=client('cloudformation').describe_stack_resources(StackName=stack_name(c,'bootstrap'))['StackResources']
            if any(x['LogicalResourceId']=='ApiLoggingRole' and existing.endswith('/'+x['PhysicalResourceId']) for x in owned):
                raise ValueError('Do not remove the active regional logging role. Migrate the account setting before disabling bootstrap logging management.')
    if kind=='bootstrap' and c['manage_api_gateway_logging'] and existing:
        previous=describe_stack(client('cloudformation'),stack_name(c,'bootstrap'))
        # The bootstrap-created role is allowed on updates; do not overwrite another stack's account setting.
        if not previous: raise ValueError('An API Gateway account logging role already exists. Set manage_api_gateway_logging=false to reuse it.')
        resources=client('cloudformation').describe_stack_resources(StackName=stack_name(c,'bootstrap'))['StackResources']
        owned=[x['PhysicalResourceId'] for x in resources if x['LogicalResourceId']=='ApiLoggingRole']
        if not any(existing.endswith('/'+name) for name in owned): raise ValueError('Existing regional logging role is not owned by this bootstrap')
    return existing


def validate_workflows(client,c,template):
    values={name:c['project_prefix']+'-'+name.removesuffix('Table').lower() for name in ['UsersTable','AuctionsTable','BidsTable','RequestsTable','OutboxTable']}
    values['HighBidTopicArn']=f"arn:aws:sns:{c['region']}:{c['expected_account_id']}:{c['project_prefix']}-high-bid"
    results={}
    for name in ['Bidding','CloseAuction']:
        definition=json.dumps(template['Resources'][name]['Properties']['Definition'])
        for key,value in values.items(): definition=definition.replace('${'+key+'}',value)
        result=client('stepfunctions').validate_state_machine_definition(definition=definition,type='EXPRESS',severity='WARNING')
        results[name]={'result':result['result'],'diagnostics':result.get('diagnostics',[])}
        if result['result']!='OK': raise ValueError(f'AWS rejected {name}: {results[name]}')
    return results


def upload_object_once(s3,bucket,key,data):
    from botocore.exceptions import ClientError
    sha=hashlib.sha256(data).hexdigest()
    try:
        obj=s3.get_object(Bucket=bucket,Key=key)
        if hashlib.sha256(obj['Body'].read()).hexdigest()!=sha:
            raise ValueError(f'Refusing to overwrite different release artifact {key}')
    except ClientError as e:
        if e.response['Error']['Code'] not in ['NoSuchKey','404']: raise
        s3.put_object(Bucket=bucket,Key=key,Body=data,IfNoneMatch='*',Metadata={'sha256':sha},ServerSideEncryption='AES256')


def upload_release(client,bucket,folder):
    folder=Path(folder);manifest=verify(folder);prefix='releases/'+manifest['release_id'];s3=client('s3')
    for name in [*manifest['files'],'manifest.json']:
        upload_object_once(s3,bucket,prefix+'/'+name,(folder/name).read_bytes())
    return prefix


def render_application(template,parameters):
    # API Gateway policy/integration updates need a NEW deployment snapshot.
    # Include actual parameter values as well as template content so changing a
    # developer CIDR or a CloudFront origin cannot leave the old policy active.
    rendered=copy.deepcopy(template)
    old=next(k for k,v in rendered['Resources'].items() if v['Type']=='AWS::ApiGateway::Deployment')
    new='ApiDeployment'+digest({'template':template,'parameters':parameters})[:20]
    rendered['Resources'][new]=rendered['Resources'].pop(old)
    rendered['Resources']['ApiStage']['Properties']['DeploymentId']={'Ref':new}
    return rendered


def review_view(response):
    keys=['ChangeSetId','StackId','StackName','ChangeSetName','ChangeSetType','Description','Parameters','Capabilities','RoleARN','Changes','NotificationARNs','RollbackConfiguration','IncludeNestedStacks']
    return {k:response[k] for k in keys if k in response}

def describe_change(cf,arn):
    response=cf.describe_change_set(ChangeSetName=arn,IncludePropertyValues=True)
    if response.get('NextToken'): raise ValueError('Paginated change set requires manual review; this tool will not apply a partial plan')
    return response


def plan(c,kind,folder,plan_path):
    manifest=verify(folder);client,identity=connect(c);cf=client('cloudformation')
    logging_role=check_logging(client,c,kind);template=read(Path(folder)/('application.json' if kind=='app' else 'bootstrap.json'))
    common={'ProjectPrefix':c['project_prefix']}
    args={};validation={};rendered_sha=None
    if kind=='bootstrap':
        params={**common,'GitHubRepository':c.get('github_repository',''),'GitHubSubjectPrefix':c.get('github_subject_prefix',''),'GitHubEnvironment':c.get('github_environment','dev'),'ExistingOidcProviderArn':c.get('existing_oidc_provider_arn',''),'ManageApiGatewayLogging':str(c['manage_api_gateway_logging']).lower(),'BudgetEmail':c.get('alert_email',''),'MonthlyBudgetUsd':str(c.get('monthly_budget_usd',10))}
        args['TemplateBody']=json.dumps(template)
    else:
        boot=describe_stack(cf,stack_name(c,'bootstrap'))
        if not boot or boot['StackStatus'] not in ['CREATE_COMPLETE','UPDATE_COMPLETE','UPDATE_ROLLBACK_COMPLETE']: raise ValueError('Bootstrap must be successfully applied first')
        bootout=outputs(boot);validation=validate_workflows(client,c,template)
        prefix=upload_release(client,bootout['ArtifactBucket'],folder)
        params={**common,'ArtifactBucket':bootout['ArtifactBucket'],'ArtifactPrefix':prefix,'RuntimeBoundaryArn':bootout['RuntimeBoundaryArn'],
            'AllowedApiCidr':c['allowed_api_cidr'],'AlertEmail':c.get('alert_email',''),'PointInTimeRecovery':str(c['point_in_time_recovery']).lower(),
            'ReserveLambdaConcurrency':str(c.get('reserve_lambda_concurrency',True)).lower()}
        rendered=render_application(template,params);data=canonical(rendered)
        rendered_sha=hashlib.sha256(data).hexdigest();key=prefix+'/application-'+rendered_sha+'.json'
        upload_object_once(client('s3'),bootout['ArtifactBucket'],key,data)
        save(Path(plan_path).with_suffix('.template.json'),rendered)
        args.update(TemplateURL=f"https://{bootout['ArtifactBucket']}.s3.{c['region']}.amazonaws.com/{key}",RoleARN=bootout['ExecutionRoleArn'])
    name=stack_name(c,kind);existing=describe_stack(cf,name)
    if existing and existing['StackStatus']=='REVIEW_IN_PROGRESS': change_type='CREATE'
    elif existing and existing['StackStatus'] in ['CREATE_COMPLETE','UPDATE_COMPLETE','UPDATE_ROLLBACK_COMPLETE']: change_type='UPDATE'
    elif existing: raise ValueError('Stack is not ready for a new reviewed change set: '+existing['StackStatus'])
    else: change_type='CREATE'
    cf.validate_template(**{k:v for k,v in args.items() if k in ['TemplateBody','TemplateURL']})
    created=cf.create_change_set(StackName=name,ChangeSetName=c['project_prefix']+'-review-'+uuid.uuid4().hex,ChangeSetType=change_type,
        Description='Auction release '+manifest['release_id'],Parameters=[{'ParameterKey':k,'ParameterValue':v} for k,v in params.items()],
        Capabilities=['CAPABILITY_NAMED_IAM'],Tags=[{'Key':'Project','Value':c['project_prefix']}],**args)
    cf.get_waiter('change_set_create_complete').wait(ChangeSetName=created['Id'],WaiterConfig={'Delay':5,'MaxAttempts':60})
    current=describe_change(cf,created['Id']);view=review_view(current)
    result={'account':identity['Account'],'region':c['region'],'kind':kind,'release_id':manifest['release_id'],
        'logging_role':logging_role,'workflow_validation':validation,'rendered_template_sha256':rendered_sha,'review':view,'review_digest':digest(view)}
    save(plan_path,result)
    print(json.dumps(result,indent=2,default=str))
    print('NOT EXECUTED. Review every change, IAM privilege, replacement and cost; apply requires the displayed digest.')


def apply(c,path,reviewed_digest):
    saved=read(path);client,identity=connect(c)
    if saved['account']!=identity['Account'] or saved['region']!=c['region']: raise ValueError('Plan identity mismatch')
    expected_name=stack_name(c,saved['kind'])
    if saved['review']['StackName']!=expected_name: raise ValueError('Plan stack mismatch')
    if not re.fullmatch('[a-f0-9]{64}',reviewed_digest) or reviewed_digest!=saved['review_digest'] or digest(saved['review'])!=reviewed_digest: raise ValueError('Reviewed plan digest mismatch')
    cf=client('cloudformation');live=describe_change(cf,saved['review']['ChangeSetId'])
    if digest(review_view(live))!=reviewed_digest: raise ValueError('AWS change set differs from reviewed plan')
    if live['Status']!='CREATE_COMPLETE' or live['ExecutionStatus']!='AVAILABLE': raise ValueError('Change set is not executable')
    cf.execute_change_set(ChangeSetName=live['ChangeSetId'],ClientRequestToken='review-'+reviewed_digest,DisableRollback=False)
    print('Exact reviewed change set submitted. Stack completion is NOT yet confirmed; run status.')


def status(c):
    client,_=connect(c);cf=client('cloudformation');result={}
    for kind in ['bootstrap','app']:
        stack=describe_stack(cf,stack_name(c,kind))
        result[kind]={'status':stack['StackStatus'],'outputs':outputs(stack)} if stack else None
    print(json.dumps(result,indent=2))


def wait_app(c):
    client,_=connect(c);cf=client('cloudformation');name=stack_name(c,'app')
    stack=describe_stack(cf,name)
    if not stack: raise ValueError('Application stack does not exist')
    if stack['StackStatus'] in ['CREATE_IN_PROGRESS','UPDATE_IN_PROGRESS','UPDATE_COMPLETE_CLEANUP_IN_PROGRESS']:
        waiter='stack_create_complete' if stack['StackStatus']=='CREATE_IN_PROGRESS' else 'stack_update_complete'
        cf.get_waiter(waiter).wait(StackName=name,WaiterConfig={'Delay':10,'MaxAttempts':90})
    final=describe_stack(cf,name)['StackStatus']
    if final not in ['CREATE_COMPLETE','UPDATE_COMPLETE']: raise ValueError('Deployment did not complete: '+final)
    print(final)


def publish(c,receipt_path,rollback_version=None):
    client,_=connect(c);stack=describe_stack(client('cloudformation'),stack_name(c,'app'))
    if not stack or stack['StackStatus'] not in ['CREATE_COMPLETE','UPDATE_COMPLETE','UPDATE_ROLLBACK_COMPLETE']: raise ValueError('Application stack must be complete')
    out=outputs(stack);s3=client('s3');bucket=out['WebBucket'];previous=None
    from botocore.exceptions import ClientError
    try: previous=s3.head_object(Bucket=bucket,Key='index.html').get('VersionId')
    except ClientError as e:
        if e.response['Error']['Code'] not in ['404','NoSuchKey']: raise
    if rollback_version:
        result=s3.copy_object(Bucket=bucket,Key='index.html',CopySource={'Bucket':bucket,'Key':'index.html','VersionId':rollback_version},MetadataDirective='REPLACE',ContentType='text/html',CacheControl='no-cache, no-store, must-revalidate')
    else:
        if not re.fullmatch(r'https://[a-z0-9]+\.execute-api\.us-east-1\.amazonaws\.com/dev',out['ApiBaseUrl']): raise ValueError('Unexpected API output URL')
        folder=ROOT/'.local/web-release';env={**os.environ,'VITE_LOCAL_MOCK':'false','VITE_REACT_APP_API_BASE':out['ApiBaseUrl'],'VITE_COGNITO_AUTHORITY':out['CognitoAuthority'],'VITE_COGNITO_CLIENT_ID':out['UserPoolClientId'],'VITE_COGNITO_DOMAIN':out['CognitoDomain']}
        subprocess.run(['bash','scripts/npm.sh','run','build','--','--outDir',str(folder),'--emptyOutDir'],cwd=ROOT,env=env,check=True)
        if not any(out['ApiBaseUrl'] in f.read_text() for f in folder.glob('assets/*.js')): raise ValueError('Built frontend does not contain the intended API endpoint')
        for file in sorted(folder.rglob('*')):
            if not file.is_file() or file.name=='index.html': continue
            key=file.relative_to(folder).as_posix()
            s3.put_object(Bucket=bucket,Key=key,Body=file.read_bytes(),ContentType=mimetypes.guess_type(key)[0] or 'application/octet-stream',CacheControl='public, max-age=31536000, immutable' if key.startswith('assets/') else 'no-cache')
        result=s3.put_object(Bucket=bucket,Key='index.html',Body=(folder/'index.html').read_bytes(),ContentType='text/html',CacheControl='no-cache, no-store, must-revalidate')
    # Save rollback evidence BEFORE invalidation, which can fail independently.
    receipt={'bucket':bucket,'previous_index_version':previous,'index_version':result.get('VersionId'),'distribution_id':out['DistributionId'],'web_url':out['WebUrl'],'invalidation':'pending'}
    save(receipt_path,receipt)
    invalidation=client('cloudfront').create_invalidation(DistributionId=out['DistributionId'],InvalidationBatch={'CallerReference':str(uuid.uuid4()),'Paths':{'Quantity':1,'Items':['/*']}})
    receipt['invalidation']=invalidation['Invalidation']['Id'];save(receipt_path,receipt)
    print(json.dumps(receipt,indent=2));print('Publication submitted; CloudFront invalidation and browser verification are still required.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['build','verify','preflight','plan-bootstrap','plan-app','apply','status','publish-web','rollback-web','wait-app'])
    parser.add_argument('--config',type=Path,default=ROOT/'infra/deploy.local.json')
    parser.add_argument('--release',type=Path);parser.add_argument('--out',type=Path)
    parser.add_argument('--plan',type=Path);parser.add_argument('--reviewed-digest');parser.add_argument('--index-version')
    args=parser.parse_args()
    if args.command=='build': print(build(args.out));return
    if args.command=='verify':
        if not args.release: parser.error('--release is required')
        print(json.dumps(verify(args.release),indent=2));return
    c=config(args.config)
    if args.command=='preflight':
        client,identity=connect(c);role=client('apigateway').get_account().get('cloudwatchRoleArn')
        print(json.dumps({'account':identity['Account'],'arn':identity['Arn'],'region':c['region'],'regional_api_logging_role':role,'resource_changes':False},indent=2));return
    if args.command.startswith('plan-'):
        if not args.release or not args.out: parser.error('--release and --out are required')
        plan(c,args.command.removeprefix('plan-'),args.release,args.out)
    elif args.command=='apply':
        if not args.plan or not args.reviewed_digest: parser.error('--plan and --reviewed-digest are required')
        apply(c,args.plan,args.reviewed_digest)
    elif args.command=='status': status(c)
    elif args.command=='wait-app': wait_app(c)
    else:
        if not args.out: parser.error('--out is required for the publication/rollback receipt')
        if args.command=='rollback-web' and not args.index_version: parser.error('--index-version is required')
        publish(c,args.out,args.index_version if args.command=='rollback-web' else None)

if __name__=='__main__':
    try: main()
    except (ValueError,FileNotFoundError) as e: raise SystemExit(str(e))
