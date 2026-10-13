import base64
from decimal import Decimal
import json
import zipfile
from pathlib import Path
import subprocess
from unittest.mock import patch

from botocore.exceptions import ClientError
import pytest
from conftest import load_lambda
from test_workflows import invoke


@pytest.mark.parametrize('value', [-1, .001, True, None, '10', 1000000001])
def test_creators_validate_money(aws, value):
    for handler, payload in [('user_post_create', {'name':'A','acctBalance':value}),
                              ('auction_post_create', {'itemName':'A','reserve':value})]:
        assert invoke(handler, payload)[0] == 400


@pytest.mark.parametrize('raw', ['null', '[]', '{', '{"name":"A","acctBalance":NaN}', '{"name":"A","acctBalance":Infinity}'])
def test_malformed_body(aws, raw):
    assert load_lambda('user_post_create').lambda_handler({'body':raw},None)['statusCode'] == 400


def test_cents_base64_and_duplicate_protection(aws):
    data = {'userId':'u','name':"O'Brien",'acctBalance':200.29}
    raw = base64.b64encode(json.dumps(data).encode()).decode()
    assert load_lambda('user_post_create').lambda_handler({'body':raw,'isBase64Encoded':True},None)['statusCode'] == 201
    assert invoke('user_post_create', {**data,'acctBalance':999})[0] == 409
    stored = aws['db'].get_item(TableName='users_table',Key={'userId':{'S':'u'}})['Item']
    assert Decimal(stored['acctBalance']['N']) == Decimal('200.29')
    assert invoke('auction_post_create', {'auctionId':'a','itemName':'A','reserve':12.34})[0] == 201
    aws['db'].update_item(TableName='auctions_table',Key={'auctionId':{'S':'a'}},UpdateExpression='SET auctionStatus = :closed',ExpressionAttributeValues={':closed':{'S':'CLOSED'}})
    assert invoke('auction_post_create', {'auctionId':'a','itemName':'B','reserve':0})[0] == 409
    assert invoke('auction_get_one', pathParameters={'auctionId':'a'})[1]['auctionStatus'] == 'CLOSED'
    assert invoke('user_get_all')[1][0]['name'] == "O'Brien"
    assert invoke('auction_get_all')[1][0]['reserve'] == 12.34


def test_get_one_and_bid_missing_inputs(aws):
    assert invoke('auction_get_one')[0] == 400
    assert invoke('auction_get_one', pathParameters={'auctionId':'missing'})[0] == 404
    assert invoke('bid_get_for_auction')[0] == 400
    assert invoke('bid_get_for_auction', queryStringParameters={'auctionId':'missing'})[0] == 404


def test_bid_query_follows_actual_service_pages(aws):
    invoke('auction_post_create', {'auctionId':'a','itemName':'A','reserve':0})
    # More than 1 MiB forces DynamoDB/Moto to return LastEvaluatedKey.
    with __import__('boto3').resource('dynamodb').Table('bids_table').batch_writer() as writer:
        for n in range(1, 7):
            writer.put_item(Item={'auctionId':'a','bidAmt':n,'userId':'u','date':'2026-10-05','padding':'x'*230000})
    status, data = invoke('bid_get_for_auction', queryStringParameters={'auctionid':'a'})
    assert status == 200
    assert [row['bidAmt'] for row in data] == [6,5,4,3,2,1]


def test_scan_pages_and_sdk_failure(aws):
    from auction_common import all_pages
    class Pages:
        def scan(self, **args):
            return {'Items':[{'userId':'b'}]} if args.get('ExclusiveStartKey') else {'Items':[{'userId':'a'}],'LastEvaluatedKey':{'userId':'a'}}
    assert len(all_pages(Pages(), 'scan')) == 2
    error = ClientError({'Error':{'Code':'AccessDeniedException','Message':'Do not expose this'}},'Scan')
    with patch('auction_common.table') as table:
        table.return_value.scan.side_effect = error
        status, response = invoke('user_get_all')
    assert status == 503 and 'Do not expose' not in json.dumps(response)


def test_publisher_failure_keeps_pending_and_pagination(aws, monkeypatch):
    from test_workflows import execution, fail
    from asl_runner import Evaluator
    invoke('user_post_create', {'userId':'alice','name':'Alice','acctBalance':100})
    invoke('auction_post_create', {'auctionId':'camera','itemName':'A','reserve':1})
    evaluator=Evaluator()
    try:
        execution(aws,evaluator,amount=2,hook=lambda run,when,args: (_ for _ in ()).throw(fail()) if run.current=='PublishNotification' else None).run()
    finally:evaluator.close()
    module=load_lambda('notification_replay')
    original_client=__import__('boto3').client
    class BrokenSNS:
        def publish(self,**args): raise fail()
    with patch.object(module.boto3,'client',side_effect=lambda name,*a,**kw: BrokenSNS() if name=='sns' else original_client(name,*a,**kw)):
        assert module.lambda_handler({},None)['failed']==1
    assert aws['db'].scan(TableName='outbox_table')['Items'][0]['deliveryStatus']['S']=='PENDING'
    # Force the publisher to traverse two GSI query pages (page limit is 25).
    for n in range(30):
        aws['db'].put_item(TableName='outbox_table',Item={'eventId':{'S':f'e{n}'},'payload':{'S':json.dumps({'eventId':f'e{n}'})},'deliveryStatus':{'S':'PENDING'},'createdAt':{'S':f'2026-10-05T00:00:{n:02d}Z'}})
    assert module.lambda_handler({},None)['sent']==31


def test_packaged_handlers_import_without_credentials_or_network(aws, tmp_path):
    root=Path(__file__).resolve().parents[2]
    subprocess.run(['python3','scripts/package_lambdas.py'],cwd=root,check=True,capture_output=True)
    for archive in (root/'.local/lambda-zips').glob('*.zip'):
        target=tmp_path/archive.stem;target.mkdir()
        with zipfile.ZipFile(archive) as z:
            assert 'auction_common.py' in z.namelist()
            z.extractall(target)
        # A fresh interpreter resolves the shared module from the zip, not test sys.path.
        script = """
import sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
with patch('boto3.resource', side_effect=AssertionError('Import accessed AWS')), patch('boto3.client', side_effect=AssertionError('Import accessed AWS')):
    import lambda_function, auction_common
    assert callable(lambda_function.lambda_handler)
    assert '.zip/auction_common.py' in auction_common.__file__
"""
        subprocess.run([str(root/'.venv/bin/python'),'-c',script,str(archive)],cwd=tmp_path,check=True,capture_output=True)


def test_replay_emits_backlog_metric_for_pending_failures(aws, monkeypatch, capsys):
    from test_workflows import fail
    aws['db'].put_item(TableName='outbox_table',Item={
        'eventId':{'S':'metric-event'},'payload':{'S':'{}'},
        'deliveryStatus':{'S':'PENDING'},'createdAt':{'S':'2020-01-01T00:00:00Z'}})
    module=load_lambda('notification_replay')
    original_client=__import__('boto3').client
    class BrokenSNS:
        def publish(self,**args): raise fail()
    with patch.object(module.boto3,'client',side_effect=lambda name,*a,**kw: BrokenSNS() if name=='sns' else original_client(name,*a,**kw)):
        result=module.lambda_handler({},None)
    assert result['failed']==1 and result['oldestPendingAgeSeconds']>900
    summary=json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary=={'event':'outbox_summary',**result}
    recovered=module.lambda_handler({},None)
    assert recovered['sent']==1 and recovered['failed']==0 and recovered['oldestPendingAgeSeconds']==0
