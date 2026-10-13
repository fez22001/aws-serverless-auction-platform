import importlib.util
from pathlib import Path
import socket
import sys

import boto3
from moto import mock_aws
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'hw10/shared'))
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def isolated_aws(monkeypatch):
    for key, value in {'AWS_ACCESS_KEY_ID': 'testing', 'AWS_SECRET_ACCESS_KEY': 'testing',
                       'AWS_DEFAULT_REGION': 'us-east-1', 'AWS_EC2_METADATA_DISABLED': 'true'}.items():
        monkeypatch.setenv(key, value)
    for name in ['AWS_PROFILE', 'AWS_SESSION_TOKEN', 'AWS_ENDPOINT_URL', 'AWS_ENDPOINT_URL_DYNAMODB', 'AWS_ENDPOINT_URL_SNS', 'AWS_ENDPOINT_URL_SQS']:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', '/dev/null')
    monkeypatch.setenv('AWS_CONFIG_FILE', '/dev/null')
    def denied(*args, **kwargs):
        raise AssertionError('All socket connections are forbidden in backend tests')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    with mock_aws():
        yield


@pytest.fixture
def aws(monkeypatch):
    db = boto3.client('dynamodb')
    names = {}
    for env, pk, sk in [('USERS_TABLE', 'userId', None), ('AUCTIONS_TABLE', 'auctionId', None),
                       ('BIDS_TABLE', 'auctionId', 'bidAmt'), ('REQUESTS_TABLE', 'requestKey', None), ('OUTBOX_TABLE', 'eventId', None)]:
        name = env.lower()
        monkeypatch.setenv(env, name)
        names[env] = name
        schema = [{'AttributeName': pk, 'KeyType': 'HASH'}]
        attrs = [{'AttributeName': pk, 'AttributeType': 'S'}]
        kwargs = {}
        if sk:
            schema.append({'AttributeName': sk, 'KeyType': 'RANGE'})
            attrs.append({'AttributeName': sk, 'AttributeType': 'N'})
        if env == 'OUTBOX_TABLE':
            attrs.extend([{'AttributeName': 'deliveryStatus', 'AttributeType': 'S'}, {'AttributeName': 'createdAt', 'AttributeType': 'S'}])
            kwargs['GlobalSecondaryIndexes'] = [{'IndexName': 'pending-by-date', 'KeySchema': [
                {'AttributeName': 'deliveryStatus', 'KeyType': 'HASH'}, {'AttributeName': 'createdAt', 'KeyType': 'RANGE'}],
                'Projection': {'ProjectionType': 'ALL'}}]
        db.create_table(TableName=name, KeySchema=schema, AttributeDefinitions=attrs, BillingMode='PAY_PER_REQUEST', **kwargs)
    sns = boto3.client('sns'); sqs = boto3.client('sqs')
    topic = sns.create_topic(Name='local-high-bids')['TopicArn']
    queue = sqs.create_queue(QueueName='local-notifications')['QueueUrl']
    arn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=['QueueArn'])['Attributes']['QueueArn']
    sns.subscribe(TopicArn=topic, Protocol='sqs', Endpoint=arn, Attributes={'RawMessageDelivery': 'true'})
    monkeypatch.setenv('HIGH_BID_TOPIC_ARN', topic)
    resources = {'UsersTable': names['USERS_TABLE'], 'AuctionsTable': names['AUCTIONS_TABLE'], 'BidsTable': names['BIDS_TABLE'],
                 'RequestsTable': names['REQUESTS_TABLE'], 'OutboxTable': names['OUTBOX_TABLE'], 'HighBidTopicArn': topic}
    return {'db': db, 'sns': sns, 'sqs': sqs, 'queue': queue, 'resources': resources, 'names': names}


def load_lambda(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f'hw10/lambdas/{name}/lambda_function.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
