from decimal import Decimal
import json
from uuid import uuid4

from botocore.exceptions import ClientError
import pytest
from asl_runner import Evaluator, Execution
from conftest import load_lambda


@pytest.fixture
def evaluator():
    result = Evaluator()
    yield result
    result.close()


def invoke(name, body=None, **event):
    response = load_lambda(name).lambda_handler({'body': json.dumps(body) if body is not None else None, **event}, None)
    return response['statusCode'], json.loads(response['body'])


@pytest.fixture
def seeded(aws):
    assert invoke('user_post_create', {'userId': 'alice', 'name': 'Alice', 'acctBalance': 500.50})[0] == 201
    assert invoke('user_post_create', {'userId': 'bob', 'name': 'Bob', 'acctBalance': 200})[0] == 201
    assert invoke('auction_post_create', {'auctionId': 'camera', 'itemName': 'Camera', 'reserve': 100})[0] == 201
    return aws


def execution(aws, evaluator, kind='Bidding', amount=120.25, user='alice', auction='camera', rid=None, hook=None):
    data = {'auctionId': auction, 'requestId': rid or str(uuid4())}
    if kind == 'Bidding': data.update(userId=user, bidAmt=amount)
    return Execution(kind, data, aws, evaluator, hook=hook)


def balance(aws, user='alice'):
    return Decimal(aws['db'].get_item(TableName='users_table', Key={'userId': {'S': user}})['Item']['acctBalance']['N'])


def rows(aws, table):
    return aws['db'].scan(TableName=table)['Items']


def fail(message='Injected fault'):
    return ClientError({'Error': {'Code': 'InternalServerError', 'Message': message}}, 'Test')


def test_real_workflows_cents_receipts_and_notification(seeded, evaluator):
    bid = execution(seeded, evaluator, rid='bid-once')
    result = bid.run()
    assert result['accepted'], result
    assert execution(seeded, evaluator, rid='bid-once').run() == result
    closed = execution(seeded, evaluator, 'CloseAuction', rid='close-once').run()
    assert closed['accepted'], closed
    assert execution(seeded, evaluator, 'CloseAuction', rid='close-once').run() == closed
    assert balance(seeded) == Decimal('380.25')
    assert len(rows(seeded, 'bids_table')) == len(rows(seeded, 'outbox_table')) == 1
    assert rows(seeded, 'outbox_table')[0]['deliveryStatus']['S'] == 'SENT'
    messages = seeded['sqs'].receive_message(QueueUrl=seeded['queue'])['Messages']
    assert json.loads(messages[0]['Body'])['eventId'] == 'BID#bid-once'
    assert execution(seeded, evaluator, amount=130).run()['reason'] == 'AuctionClosed'


@pytest.mark.parametrize('payload', [None, [], {}, {'auctionId': 'camera', 'userId': 'alice', 'bidAmt': '12'},
    {'auctionId': 'camera', 'userId': 'alice', 'bidAmt': True},
    {'auctionId': 'camera', 'userId': 'alice', 'bidAmt': -1},
    {'auctionId': 'camera', 'userId': 'alice', 'bidAmt': .001},
    {'auctionId': 'camera', 'userId': 'alice', 'bidAmt': 1e10},
    {'auctionId': 'camera', 'userId': 'alice', 'bidAmt': 12, 'requestId': ''}])
def test_invalid_inputs_do_not_write(seeded, evaluator, payload):
    result = Execution('Bidding', payload, seeded, evaluator).run()
    assert result['httpStatus'] == 400
    assert rows(seeded, 'requests_table') == []
    assert rows(seeded, 'bids_table') == []


@pytest.mark.parametrize('kwargs,reason', [({'user':'missing'},'invalid_user'), ({'auction':'missing'},'AuctionNotExist'),
                                         ({'amount':501},'insufficient_funds')])
def test_business_rejections(seeded, evaluator, kwargs, reason):
    assert execution(seeded, evaluator, **kwargs).run()['reason'] == reason
    assert balance(seeded) == Decimal('500.50')
    assert rows(seeded, 'outbox_table') == []


def test_reserve_and_equal_bid(seeded, evaluator):
    assert execution(seeded, evaluator, 'CloseAuction').run()['reason'] == 'BidNotExist'
    assert execution(seeded, evaluator, amount=90).run()['accepted']
    assert execution(seeded, evaluator, 'CloseAuction').run()['reason'] == 'ReserveNotMet'
    assert execution(seeded, evaluator, amount=90).run()['reason'] == 'invalid_bid'
    assert execution(seeded, evaluator, amount=89).run()['reason'] == 'invalid_bid'


def test_same_request_id_different_payload(seeded, evaluator):
    assert execution(seeded, evaluator, rid='same').run()['accepted']
    assert execution(seeded, evaluator, rid='same', amount=130).run()['reason'] == 'IdempotencyConflict'
    assert len(rows(seeded, 'bids_table')) == 1


def test_interleaved_equal_bids_only_one_winner(seeded, evaluator):
    a = execution(seeded, evaluator, user='alice').until('Commit')
    b = execution(seeded, evaluator, user='bob').until('Commit')
    assert a.run()['accepted']
    assert b.run()['reason'] == 'invalid_bid'
    assert len(rows(seeded, 'bids_table')) == 1
    assert rows(seeded, 'bids_table')[0]['userId']['S'] == 'alice'


def test_higher_bid_retries_stale_version(seeded, evaluator):
    a = execution(seeded, evaluator, amount=120).until('Commit')
    b = execution(seeded, evaluator, amount=130).until('Commit')
    assert a.run()['accepted']
    assert b.run()['accepted']
    assert execution(seeded, evaluator, 'CloseAuction').run()['winningBid'] == 130
    assert balance(seeded) == Decimal('370.50')


def test_interleaved_closes_debit_once(seeded, evaluator):
    execution(seeded, evaluator).run()
    a = execution(seeded, evaluator, 'CloseAuction').until('Commit')
    b = execution(seeded, evaluator, 'CloseAuction').until('Commit')
    assert a.run()['accepted']
    assert b.run()['reason'] == 'AuctionClosed'
    assert balance(seeded) == Decimal('380.25')


@pytest.mark.parametrize('close_first', [True, False])
def test_close_vs_bid_serializes_on_auction(seeded, evaluator, close_first):
    execution(seeded, evaluator).run()
    closing = execution(seeded, evaluator, 'CloseAuction').until('Commit')
    bidding = execution(seeded, evaluator, amount=150, user='bob').until('Commit')
    if close_first:
        assert closing.run()['winningUserId'] == 'alice'
        assert bidding.run()['reason'] == 'AuctionClosed'
    else:
        assert bidding.run()['accepted']
        assert closing.run()['winningUserId'] == 'bob'
        assert balance(seeded, 'bob') == 50
        assert balance(seeded) == Decimal('500.50')


def test_concurrent_wins_cannot_overdraw(seeded, evaluator):
    invoke('auction_post_create', {'auctionId': 'second', 'itemName': 'Second', 'reserve': 1})
    execution(seeded, evaluator, amount=400).run()
    execution(seeded, evaluator, amount=400, auction='second').run()
    a = execution(seeded, evaluator, 'CloseAuction').until('Commit')
    b = execution(seeded, evaluator, 'CloseAuction', auction='second').until('Commit')
    assert a.run()['accepted']
    assert b.run()['reason'] == 'insufficient_funds'
    assert balance(seeded) == Decimal('100.50')
    assert invoke('auction_get_one', pathParameters={'auctionId': 'second'})[1]['auctionStatus'] == 'OPEN'


def test_lost_transaction_response_uses_durable_receipt(seeded, evaluator):
    execution(seeded, evaluator).run()
    def hook(run, when, args):
        if run.current == 'Commit' and when == 'after': raise fail('Response lost after commit')
    closed = execution(seeded, evaluator, 'CloseAuction', rid='lost', hook=hook).run()
    assert closed['accepted']
    assert balance(seeded) == Decimal('380.25')
    assert execution(seeded, evaluator, 'CloseAuction', rid='lost').run() == closed


def test_transaction_failure_leaves_no_partial_changes(seeded, evaluator):
    execution(seeded, evaluator).run()
    closing = execution(seeded, evaluator, 'CloseAuction').until('Commit')
    # A concurrent withdrawal makes the transaction's balance condition fail.
    seeded['db'].update_item(TableName='users_table', Key={'userId': {'S':'alice'}},
        UpdateExpression='SET acctBalance = :zero', ExpressionAttributeValues={':zero':{'N':'0'}})
    assert closing.run()['reason'] == 'insufficient_funds'
    assert invoke('auction_get_one', pathParameters={'auctionId':'camera'})[1]['auctionStatus'] == 'OPEN'
    assert len(rows(seeded, 'requests_table')) == 1  # bid only; no close receipt


def test_sns_outage_is_recoverable(seeded, evaluator):
    def hook(run, when, args):
        if run.current == 'PublishNotification': raise fail('SNS unavailable')
    assert execution(seeded, evaluator, rid='sns-failure', hook=hook).run()['accepted']
    assert rows(seeded, 'outbox_table')[0]['deliveryStatus']['S'] == 'PENDING'
    assert load_lambda('notification_replay').lambda_handler({}, None)['sent'] == 1
    assert rows(seeded, 'outbox_table')[0]['deliveryStatus']['S'] == 'SENT'
    assert load_lambda('notification_replay').lambda_handler({}, None)['sent'] == 0
    assert json.loads(seeded['sqs'].receive_message(QueueUrl=seeded['queue'])['Messages'][0]['Body'])['eventId'] == 'BID#sns-failure'


def test_publish_ack_failure_can_duplicate_same_event_id(seeded, evaluator):
    def hook(run, when, args):
        if run.current == 'MarkSent': raise fail('Marking failed')
    execution(seeded, evaluator, rid='duplicate-event', hook=hook).run()
    load_lambda('notification_replay').lambda_handler({}, None)
    messages = seeded['sqs'].receive_message(QueueUrl=seeded['queue'], MaxNumberOfMessages=10)['Messages']
    assert len(messages) == 2
    assert {json.loads(m['Body'])['eventId'] for m in messages} == {'BID#duplicate-event'}


def test_legacy_schema_fails_closed(seeded, evaluator):
    seeded['db'].update_item(TableName='auctions_table', Key={'auctionId':{'S':'camera'}}, UpdateExpression='REMOVE schemaVersion')
    assert execution(seeded, evaluator).run()['reason'] == 'MigrationRequired'
    assert execution(seeded, evaluator, 'CloseAuction').run()['reason'] == 'MigrationRequired'
    assert not rows(seeded, 'bids_table')


def test_persistent_service_failure_is_bounded(seeded, evaluator):
    def hook(run, when, args):
        if run.current == 'Commit': raise fail('Outage')
    run = execution(seeded, evaluator, hook=hook)
    assert run.run()['httpStatus'] == 503
    assert run.history.count('Commit') == 3
    assert not rows(seeded, 'bids_table')


def test_concurrent_identical_request_returns_same_receipt(seeded, evaluator):
    a=execution(seeded,evaluator,rid='racing-retry').until('Commit')
    b=execution(seeded,evaluator,rid='racing-retry').until('Commit')
    first=a.run()
    assert b.run()==first
    assert len(rows(seeded,'bids_table'))==1
    assert len(rows(seeded,'requests_table'))==1
    assert len(rows(seeded,'outbox_table'))==1


def test_different_payloads_racing_on_one_request_id_conflict(seeded,evaluator):
    a=execution(seeded,evaluator,rid='racing-id',amount=110).until('Commit')
    b=execution(seeded,evaluator,rid='racing-id',amount=120).until('Commit')
    assert a.run()['accepted']
    assert b.run()['reason']=='IdempotencyConflict'
    assert len(rows(seeded,'bids_table'))==1


def test_lost_bid_ack_leaves_notification_for_recovery(seeded,evaluator):
    def hook(run,when,args):
        if run.current=='Commit' and when=='after': raise fail('Commit acknowledgement lost')
    assert execution(seeded,evaluator,hook=hook).run()['accepted']
    assert rows(seeded,'outbox_table')[0]['deliveryStatus']['S']=='PENDING'
    assert load_lambda('notification_replay').lambda_handler({},None)['sent']==1


def test_duplicate_receipt_survives_future_auction_changes(seeded,evaluator):
    original=execution(seeded,evaluator,rid='old-bid',amount=110).run()
    execution(seeded,evaluator,amount=120).run()
    execution(seeded,evaluator,'CloseAuction').run()
    assert execution(seeded,evaluator,rid='old-bid',amount=110).run()==original
    assert len(rows(seeded,'bids_table'))==2


def test_amount_boundary_valid_cents(seeded,evaluator):
    assert execution(seeded,evaluator,amount=100.29).run()['accepted']
    assert execution(seeded,evaluator,'CloseAuction').run()['winningBid']==100.29
    assert balance(seeded)==Decimal('400.21')
