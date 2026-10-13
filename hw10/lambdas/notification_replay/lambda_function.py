"""Scheduled recovery publisher for durable outbox records; at-least-once.

A crash after Publish but before marking SENT can redeliver the same eventId.
Pending records must never be TTL-expired. The application stack provisions the GSI and schedule.
"""
import json
from datetime import datetime, timezone
import logging
import os
import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

LOG = logging.getLogger(__name__)


def lambda_handler(event, context):
    table = boto3.resource('dynamodb').Table(os.environ['OUTBOX_TABLE'])
    sns = boto3.client('sns')
    limit = min(1000, max(1, int(os.environ.get('MAX_PUBLISH_PER_RUN', '100'))))
    count = sent = failed = 0
    query = {'IndexName': 'pending-by-date', 'KeyConditionExpression': Key('deliveryStatus').eq('PENDING'),
             'Limit': min(25, limit)}
    while count < limit:
        page = table.query(**query)
        for candidate in page.get('Items', []):
            count += 1
            if count > limit:
                break
            try:
                # GSI reads may be stale; never knowingly publish a SENT record.
                item = table.get_item(Key={'eventId': candidate['eventId']}, ConsistentRead=True).get('Item')
                if not item or item['deliveryStatus'] != 'PENDING':
                    continue
                sns.publish(TopicArn=os.environ['HIGH_BID_TOPIC_ARN'], Message=item['payload'])
                table.update_item(Key={'eventId': item['eventId']},
                    UpdateExpression='SET deliveryStatus = :sent',
                    ConditionExpression='deliveryStatus = :pending',
                    ExpressionAttributeValues={':sent': 'SENT', ':pending': 'PENDING'})
                sent += 1
            except (ClientError, BotoCoreError):
                failed += 1
                LOG.exception('Outbox delivery remains recoverable')
        if not page.get('LastEvaluatedKey'):
            break
        query['ExclusiveStartKey'] = page['LastEvaluatedKey']
        query['Limit'] = min(25, limit - count) if count < limit else 1
    # GSI is eventually consistent; two alarm periods reduce stale-index noise.
    oldest = table.query(IndexName='pending-by-date',
        KeyConditionExpression=Key('deliveryStatus').eq('PENDING'), Limit=1,
        ScanIndexForward=True).get('Items', [])
    age = 0
    if oldest:
        created = datetime.fromisoformat(oldest[0]['createdAt'].replace('Z', '+00:00'))
        age = max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
    summary = {'attempted': min(count, limit), 'sent': sent, 'failed': failed,
               'oldestPendingAgeSeconds': age}
    # CloudWatch metric filters consume this line, including caught publish failures.
    print(json.dumps({'event': 'outbox_summary', **summary}))
    return summary
