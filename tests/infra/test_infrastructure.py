"""Offline contract tests: generated wiring, data retention, deployment identity and review gates."""
import copy
import hashlib
import json
from pathlib import Path
import socket
import sys
from unittest.mock import Mock

import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
import build_infra as infra
import release

@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def fail(*args,**kwargs): raise AssertionError('Infrastructure tests must not access the network')
    monkeypatch.setattr(socket.socket,'connect',fail)

@pytest.fixture
def app(): return infra.application()

@pytest.fixture
def cfg():
    return {**release.read(ROOT/'infra/deploy.example.json'),'expected_account_id':'111122223333','allowed_api_cidr':'8.8.8.8/32'}


def test_generated_templates_are_current(app):
    assert release.read(ROOT/'infra/application.json')==app
    assert release.read(ROOT/'infra/bootstrap.json')==infra.bootstrap()


def test_resource_inventory_and_durable_data(app):
    r=app['Resources']; tables={k:v for k,v in r.items() if v['Type']=='AWS::DynamoDB::Table'}
    assert len(tables)==5
    assert sum(v['Type']=='AWS::Lambda::Function' for v in r.values())==7
    assert sum(v['Type']=='AWS::StepFunctions::StateMachine' for v in r.values())==2
    for v in tables.values():
        assert v['DeletionPolicy']==v['UpdateReplacePolicy']=='Retain'
        assert 'TimeToLiveSpecification' not in v['Properties']
        assert v['Properties']['BillingMode']=='PAY_PER_REQUEST'
    assert tables['BidsTable']['Properties']['KeySchema'][1]=={'AttributeName':'bidAmt','KeyType':'RANGE'}
    gsi=tables['OutboxTable']['Properties']['GlobalSecondaryIndexes'][0]
    assert gsi['IndexName']=='pending-by-date' and gsi['Projection']['ProjectionType']=='ALL'


def test_actual_workflows_and_vtl_are_embedded(app):
    r=app['Resources'];paths=r['RestApi']['Properties']['Body']['paths']
    for name in ['Bidding','CloseAuction']:
        p=r[name]['Properties']
        assert p['StateMachineType']=='EXPRESS'
        assert p['Definition']==release.read(ROOT/f'hw10/step_functions/{name}.json')
        assert p['LoggingConfiguration']['IncludeExecutionData'] is False
    for path,method,mapping in [('/bid','post','bidPOST'),('/auction','put','auctionPUT')]:
        i=paths[path][method]['x-amazon-apigateway-integration']
        assert i['uri']['Fn::Sub'].endswith('states:action/StartSyncExecution')
        assert i['requestTemplates']['application/json']==(ROOT/f'hw10/apigw/request_mappings/{mapping}.vtl').read_text()
        assert i['responses']['2\\d{2}']['responseTemplates']['application/json']==(ROOT/f'hw10/apigw/response_mappings/{mapping}.vtl').read_text()
        assert i['responses']['default']['statusCode']=='503'
    actual={(path,method) for path,methods in paths.items() for method in methods if method!='options'}
    assert actual=={('/user','get'),('/user','post'),('/auction','get'),('/auction','post'),('/auction','put'),('/auction/{auctionId}','get'),('/bid','get'),('/bid','post')}
    assert all('options' in methods for methods in paths.values())


def test_api_cidr_and_private_frontend(app):
    r=app['Resources'];policy=r['RestApi']['Properties']['Body']['x-amazon-apigateway-policy']
    deny=policy['Statement'][1]
    assert deny['Effect']=='Deny' and deny['Condition']['NotIpAddress']['aws:SourceIp']==infra.ref('AllowedApiCidr')
    assert all(r['WebBucket']['Properties']['PublicAccessBlockConfiguration'].values())
    assert r['WebOac']['Properties']['OriginAccessControlConfig']['SigningBehavior']=='always'
    assert 'CustomErrorResponses' not in r['WebDistribution']['Properties']['DistributionConfig']
    assert 'bidding' in r['SpaRouter']['Properties']['FunctionCode']
    assert r['ApiStage']['Properties']['MethodSettings'][0]['DataTraceEnabled'] is False


def test_runtime_roles_have_boundaries_and_no_deployment_power(app):
    for r in app['Resources'].values():
        if r['Type']!='AWS::IAM::Role': continue
        p=r['Properties'];assert p['PermissionsBoundary']==infra.ref('RuntimeBoundaryArn')
        for stmt in p['Policies'][0]['PolicyDocument']['Statement']:
            actions=stmt['Action'] if isinstance(stmt['Action'],list) else [stmt['Action']]
            assert not any(a.startswith(('iam:','cloudformation:')) or a=='*' or a.endswith(':*') for a in actions)
            if stmt['Resource']=='*': assert set(actions)<=set(infra.LOG_DELIVERY)
    bidding=app['Resources']['BiddingRole']['Properties']['Policies'][0]['PolicyDocument']
    assert 'dynamodb:ConditionCheckItem' in json.dumps(bidding)
    assert 'dynamodb:TransactWriteItems' not in json.dumps(bidding)


def test_bootstrap_oidc_and_artifact_write_once():
    r=infra.bootstrap()['Resources']
    trust=r['GitHubDeployRole']['Properties']['AssumeRolePolicyDocument']['Statement'][0]
    assert trust['Condition']['StringEquals']['token.actions.githubusercontent.com:aud']=='sts.amazonaws.com'
    assert trust['Condition']['StringEquals']['token.actions.githubusercontent.com:sub']==infra.sub('repo:${GitHubRepository}:environment:${GitHubEnvironment}')
    deny=r['ArtifactsPolicy']['Properties']['PolicyDocument']['Statement'][1]
    assert deny['Condition']['Null']['s3:if-none-match']=='true'
    assert r['ApiLoggingAccount']['Condition']=='ManageLogging'
    assert r['Artifacts']['DeletionPolicy']=='Retain'


def test_api_policy_management_is_scoped_to_deployment_role():
    resources=infra.bootstrap()['Resources']
    document=resources['CloudFormationRole']['Properties']['Policies'][0]['PolicyDocument']
    grants=[s for s in document['Statement'] if s['Action']=='apigateway:UpdateRestApiPolicy']
    assert len(grants)==1
    assert grants[0]['Resource']==infra.sub('arn:${AWS::Partition}:apigateway:${AWS::Region}::/restapis/*')
    assert 'apigateway:UpdateRestApiPolicy' not in json.dumps(resources['RuntimeBoundary'])


def test_outbox_monitoring_catches_handled_errors(app):
    r=app['Resources']
    assert r['Replay']['Properties']['ReservedConcurrentExecutions']=={'Fn::If':['ReserveConcurrency',1,infra.ref('AWS::NoValue')]}
    assert r['ReplaySchedule']['Properties']['ScheduleExpression']=='rate(5 minutes)'
    assert r['OutboxFailuresFilter']['Properties']['MetricTransformations'][0]['MetricValue']=='$.failed'
    assert r['OutboxOldestAgeFilter']['Properties']['MetricTransformations'][0]['MetricValue']=='$.oldestPendingAgeSeconds'
    assert r['ReplayAsync']['Properties']['DestinationConfig']['OnFailure']['Destination']==infra.att('ReplayDlq')
    assert r['NotificationSubscription']['Properties']['RedrivePolicy']['deadLetterTargetArn']==infra.att('NotificationDlq')


def test_releases_reproducible_and_detect_tampering(tmp_path):
    first=release.build(tmp_path/'a');second=release.build(tmp_path/'b')
    assert first.name==second.name
    assert release.verify(first)==release.verify(second)
    target=first/'user_get_all.zip';target.write_bytes(target.read_bytes()+b'changed')
    with pytest.raises(ValueError,match='hash mismatch'): release.verify(first)

@pytest.mark.parametrize('field,value',[
    ('expected_account_id','old-account'),('region','us-west-2'),('allowed_api_cidr','8.8.8.0/24'),
    ('allowed_api_cidr','203.0.113.1/32'),('allowed_api_cidr','127.0.0.1/32'),
    ('point_in_time_recovery','false'),('manage_api_gateway_logging','true'),('reserve_lambda_concurrency','false'),('project_prefix','unsafe/*')])
def test_config_rejects_placeholder_or_unsafe_values(tmp_path,cfg,field,value):
    cfg[field]=value;path=tmp_path/'config.json';release.save(path,cfg)
    with pytest.raises(ValueError): release.config(path)


def test_config_accepts_explicit_identity_and_cidr(tmp_path,cfg):
    path=tmp_path/'config.json';release.save(path,cfg)
    assert release.config(path)==cfg


def test_identity_mismatch_precedes_mutations(monkeypatch,cfg):
    import boto3
    session=Mock();session.client.return_value.get_caller_identity.return_value={'Account':'999900001111','Arn':'test'}
    monkeypatch.setattr(boto3,'Session',Mock(return_value=session))
    with pytest.raises(ValueError,match='account mismatch'): release.connect(cfg)
    assert session.client.call_count==1
    assert session.client.call_args.args[0]=='sts'


def test_endpoint_override_rejected(monkeypatch,cfg):
    monkeypatch.setenv('AWS_ENDPOINT_URL','http://localhost:9999')
    with pytest.raises(ValueError,match='overrides'): release.connect(cfg)


def test_logging_role_never_overwrites_unowned_setting(cfg):
    cfg['manage_api_gateway_logging']=True
    gateway=Mock();gateway.get_account.return_value={'cloudwatchRoleArn':'arn:aws:iam::111122223333:role/existing'}
    cf=Mock();cf.describe_stacks.return_value={'Stacks':[{'StackName':'auction-dev-bootstrap'}]};cf.describe_stack_resources.return_value={'StackResources':[]}
    with pytest.raises(ValueError,match='not owned'): release.check_logging(lambda n:gateway if n=='apigateway' else cf,cfg,'bootstrap')

@pytest.fixture
def review():
    return {'ChangeSetId':'arn:aws:cloudformation:us-east-1:111122223333:changeSet/review-1/abc',
        'StackName':'auction-dev-app','ChangeSetName':'review-1','Changes':[{'ResourceChange':{'Action':'Add','LogicalResourceId':'Replay'}}],
        'Status':'CREATE_COMPLETE','ExecutionStatus':'AVAILABLE'}

@pytest.mark.parametrize('failure',['digest','changed','unavailable','account','region','stack'])
def test_apply_rejects_unreviewed_or_stale_plan(tmp_path,monkeypatch,cfg,review,failure):
    cf=Mock();cf.describe_change_set.return_value=copy.deepcopy(review)
    monkeypatch.setattr(release,'connect',lambda c:(lambda n:cf,{'Account':cfg['expected_account_id']}))
    view=release.review_view(review);sha=release.digest(view)
    saved={'account':cfg['expected_account_id'],'region':'us-east-1','kind':'app','review':view,'review_digest':sha}
    if failure=='digest': sha='f'*64
    elif failure=='changed': cf.describe_change_set.return_value['Changes']=[]
    elif failure=='unavailable': cf.describe_change_set.return_value['ExecutionStatus']='EXECUTE_COMPLETE'
    elif failure=='account': saved['account']='999900001111'
    elif failure=='region': saved['region']='us-west-2'
    elif failure=='stack': saved['review']['StackName']='another-app'
    path=tmp_path/'plan.json';release.save(path,saved)
    with pytest.raises(ValueError):release.apply(cfg,path,sha)
    cf.execute_change_set.assert_not_called()


def test_apply_only_executes_exact_reviewed_change_set(tmp_path,monkeypatch,cfg,review):
    cf=Mock();cf.describe_change_set.return_value=review
    monkeypatch.setattr(release,'connect',lambda c:(lambda n:cf,{'Account':cfg['expected_account_id']}))
    view=release.review_view(review);sha=release.digest(view);path=tmp_path/'plan.json'
    release.save(path,{'account':cfg['expected_account_id'],'region':'us-east-1','kind':'app','review':view,'review_digest':sha})
    release.apply(cfg,path,sha)
    cf.execute_change_set.assert_called_once_with(ChangeSetName=review['ChangeSetId'],ClientRequestToken='review-'+sha,DisableRollback=False)


def test_incomplete_changeset_not_reviewable(review):
    cf=Mock();cf.describe_change_set.return_value={**review,'NextToken':'next'}
    with pytest.raises(ValueError,match='partial'): release.describe_change(cf,review['ChangeSetId'])


def test_upload_is_create_only_and_refuses_modified_objects(tmp_path):
    from botocore.exceptions import ClientError
    from io import BytesIO
    folder=release.build(tmp_path/'release')
    s3=Mock();s3.get_object.side_effect=ClientError({'Error':{'Code':'NoSuchKey','Message':'missing'}},'GetObject')
    prefix=release.upload_release(lambda n:s3,'artifact-bucket',folder)
    assert prefix=='releases/'+folder.name
    assert s3.put_object.call_count==10
    assert all(call.kwargs['IfNoneMatch']=='*' for call in s3.put_object.call_args_list)
    s3.reset_mock();s3.get_object.side_effect=None;s3.get_object.return_value={'Body':BytesIO(b'altered')}
    with pytest.raises(ValueError,match='Refusing to overwrite'): release.upload_release(lambda n:s3,'artifact-bucket',folder)
    s3.put_object.assert_not_called()


def test_cloud_validation_failure_stops_before_plan(cfg,app):
    sf=Mock();sf.validate_state_machine_definition.return_value={'result':'FAIL','diagnostics':[{'code':'SCHEMA_VALIDATION_FAILED'}]}
    with pytest.raises(ValueError,match='AWS rejected'): release.validate_workflows(lambda n:sf,cfg,app)
    sf.create_change_set.assert_not_called()


def test_budget_optional_account_wide_alert_not_a_spending_cap():
    budget=infra.bootstrap()['Resources']['AccountBudget']
    assert budget['Condition']=='BudgetEnabled'
    assert budget['Properties']['Budget']['TimeUnit']=='MONTHLY'
    assert 'CostFilters' not in budget['Properties']['Budget']
    assert [n['Notification']['Threshold'] for n in budget['Properties']['NotificationsWithSubscribers']]==[50,80,100]


def test_logging_role_cannot_be_removed_while_region_uses_it(cfg):
    gateway=Mock();gateway.get_account.return_value={'cloudwatchRoleArn':'arn:aws:iam::111122223333:role/owned-role'}
    cf=Mock();cf.describe_stacks.return_value={'Stacks':[{'StackName':'auction-dev-bootstrap'}]}
    cf.describe_stack_resources.return_value={'StackResources':[{'LogicalResourceId':'ApiLoggingRole','PhysicalResourceId':'owned-role'}]}
    with pytest.raises(ValueError,match='Do not remove'): release.check_logging(lambda n:gateway if n=='apigateway' else cf,cfg,'bootstrap')


def test_only_new_workflows_are_active_and_deployment_is_manual():
    import yaml
    active=ROOT/'.github/workflows'
    assert {p.name for p in active.iterdir()}=={'auction-ci.yml','hw10-deploy.yml'}
    # BaseLoader avoids YAML 1.1 treating GitHub's `on` key as a boolean.
    deploy=yaml.load((active/'hw10-deploy.yml').read_text(),Loader=yaml.BaseLoader)
    assert set(deploy['on'])=={'workflow_dispatch'}
    job=deploy['jobs']['deploy']
    assert job['environment']=='dev' and job['if']=="github.ref == 'refs/heads/main'"
    assert job['permissions']['id-token']=='write'
    ci=yaml.load((active/'auction-ci.yml').read_text(),Loader=yaml.BaseLoader)
    assert 'id-token' not in ci['permissions']


def test_bootstrap_plan_creates_review_without_executing(tmp_path,monkeypatch,cfg,review):
    folder=release.build(tmp_path/'release');cf=Mock()
    monkeypatch.setattr(release,'connect',lambda c:(lambda n:cf,{'Account':cfg['expected_account_id']}))
    monkeypatch.setattr(release,'check_logging',lambda *a:None)
    monkeypatch.setattr(release,'describe_stack',lambda *a:None)
    cf.create_change_set.return_value={'Id':'review-arn'}
    cf.describe_change_set.return_value={**review,'StackName':'auction-dev-bootstrap'}
    planfile=tmp_path/'plan.json'
    release.plan(cfg,'bootstrap',folder,planfile)
    assert cf.create_change_set.call_args.kwargs['ChangeSetType']=='CREATE'
    assert 'TemplateBody' in cf.create_change_set.call_args.kwargs
    assert 'RoleARN' not in cf.create_change_set.call_args.kwargs
    cf.execute_change_set.assert_not_called()
    saved=release.read(planfile)
    assert release.digest(saved['review'])==saved['review_digest']


def test_application_plan_validates_uploads_and_pins_execution_role(tmp_path,monkeypatch,cfg,review):
    folder=release.build(tmp_path/'release');cf=Mock()
    monkeypatch.setattr(release,'connect',lambda c:(lambda n:cf,{'Account':cfg['expected_account_id']}))
    monkeypatch.setattr(release,'check_logging',lambda *a:'existing-role')
    boot={'StackStatus':'CREATE_COMPLETE','Outputs':[{'OutputKey':k,'OutputValue':v} for k,v in {
        'ArtifactBucket':'artifact-bucket','ExecutionRoleArn':'arn:aws:iam::111122223333:role/cfn','RuntimeBoundaryArn':'boundary'}.items()]}
    monkeypatch.setattr(release,'describe_stack',lambda cf,name:boot if name.endswith('bootstrap') else None)
    calls=[]
    monkeypatch.setattr(release,'upload_object_once',lambda *a:None)
    monkeypatch.setattr(release,'validate_workflows',lambda *a:calls.append('validate') or {'Bidding':{'result':'OK'}})
    monkeypatch.setattr(release,'upload_release',lambda *a:calls.append('upload') or 'releases/'+folder.name)
    cf.create_change_set.return_value={'Id':'review-arn'};cf.describe_change_set.return_value=review
    release.plan(cfg,'app',folder,tmp_path/'plan.json')
    assert calls==['validate','upload']
    kwargs=cf.create_change_set.call_args.kwargs
    assert kwargs['RoleARN']=='arn:aws:iam::111122223333:role/cfn'
    assert '/application-' in kwargs['TemplateURL'] and kwargs['TemplateURL'].endswith('.json')
    assert 'TemplateBody' not in kwargs
    cf.execute_change_set.assert_not_called()


def test_changed_cidr_forces_new_api_deployment_snapshot(app):
    first=release.render_application(app,{'AllowedApiCidr':'8.8.8.8/32'})
    second=release.render_application(app,{'AllowedApiCidr':'1.1.1.1/32'})
    first_id=first['Resources']['ApiStage']['Properties']['DeploymentId']['Ref']
    second_id=second['Resources']['ApiStage']['Properties']['DeploymentId']['Ref']
    assert first_id!=second_id
    assert first_id in first['Resources'] and first_id not in second['Resources']
    assert release.render_application(app,{'AllowedApiCidr':'8.8.8.8/32'})==first
    assert len(first['Resources'])==len(app['Resources'])


def test_every_public_api_requires_cognito_except_preflight(app):
    r=app['Resources'];body=r['RestApi']['Properties']['Body']
    for path in body['paths'].values():
        for method, operation in path.items():
            assert operation.get('security') == (None if method=='options' else [{'Cognito':[]}])
    authorizer=body['securityDefinitions']['Cognito']['x-amazon-apigateway-authorizer']
    assert authorizer['identityValidationExpression']==infra.sub('^${UserPoolClient}$')
    assert r['UserPool']['Properties']['AdminCreateUserConfig']['AllowAdminCreateUserOnly'] is True
    client=r['UserPoolClient']['Properties']
    assert client['GenerateSecret'] is False and client['AllowedOAuthFlows']==['code']
    assert client['IdTokenValidity']==15
    for name in ['UserCreate','UserGetAll','AuctionCreate','AuctionGetOne','AuctionGetAll','BidGet']:
        assert r[name]['Properties']['Environment']['Variables']['REQUIRE_AUTH']=='true'
