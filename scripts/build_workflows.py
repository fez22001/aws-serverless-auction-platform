"""Generate reviewable JSONata ASL. No AWS calls. Run after editing this builder."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def expr(source):
    return '{% ' + source + ' %}'


def attr(source, kind='S'):
    return {kind: expr(source)}


def literal(value, kind='S'):
    return {kind: str(value)}


def choice(rules, default):
    return {'Type': 'Choice', 'Choices': [{'Condition': expr(c), 'Next': n} for c, n in rules], 'Default': default}


def task(operation, args, next_state, assign=None, catch='Unavailable'):
    state = {'Type': 'Task', 'Resource': 'arn:aws:states:::aws-sdk:dynamodb:' + operation,
             'Arguments': args, 'Next': next_state,
             'Retry': [{'ErrorEquals': ['States.TaskFailed'], 'IntervalSeconds': 1, 'MaxAttempts': 2, 'BackoffRate': 2}],
             'Catch': [{'ErrorEquals': ['States.ALL'], 'Next': catch}]}
    if assign:
        state['Assign'] = {k: expr(v) for k, v in assign.items()}
    return state


def reject(reason, message, status=200, retryable=False):
    return {'Type': 'Pass', 'End': True, 'Output': {'accepted': False, 'reason': reason,
            'message': message, 'httpStatus': status, 'retryable': retryable}}


def string_id(source, maxlen=64):
    return f"($type({source}) = 'string' ? $exists($match({source}, /^[A-Za-z0-9_-]{{1,{maxlen}}}$/)) : false)"


def generate(bidding):
    operation = 'BID' if bidding else 'CLOSE'
    valid = string_id('$states.input.auctionId')
    valid += " and ($exists($states.input.requestId) ? " + string_id('$states.input.requestId', 128) + ' : true)'
    if bidding:
        valid += ' and ' + string_id('$states.input.userId')
        valid += " and ($type($states.input.bidAmt) = 'number' ? ($states.input.bidAmt > 0 and $states.input.bidAmt <= 1000000000 and $round($states.input.bidAmt, 2) = $states.input.bidAmt) : false)"
    s = {
        'Validate': choice([(valid, 'Initialize')], 'InvalidInput'),
        'Initialize': {'Type': 'Pass', 'Assign': {
            'aid': expr('$states.input.auctionId'),
            'rid': expr('$exists($states.input.requestId) ? $states.input.requestId : $states.context.Execution.Id'),
            'principal': expr("$exists($states.input._principal) ? $states.input._principal : ''"),
            'attempt': 0, 'createdAt': expr('$now()')}, 'Next': 'MakeIdentity'},
        'MakeIdentity': {'Type': 'Pass', 'Assign': {'requestKey': expr(f"'{operation}#' & ($principal != '' ? $principal & '#' : '') & $rid")}, 'Next': 'ReadReceipt'},
        'ReadReceipt': task('getItem', {'TableName': '${RequestsTable}', 'Key': {'requestKey': attr('$requestKey')}, 'ConsistentRead': True},
                            'ReceiptExists', {'receipt': '$exists($states.result.Item) ? $states.result.Item : {}'}),
        'ReceiptExists': choice([('$exists($receipt.requestKey.S)', 'MatchReceipt')], 'AttemptLimit'),
        'MatchReceipt': choice([('$receipt.fingerprint.S = $fingerprint', 'ReturnReceipt')], 'RequestConflict'),
        'ReturnReceipt': {'Type': 'Pass', 'End': True, 'Output': expr('$parse($receipt.response.S)')},
        'AttemptLimit': choice([('$attempt >= 3', 'Unavailable')], 'ReadAuction'),
        'ReadAuction': task('getItem', {'TableName': '${AuctionsTable}', 'Key': {'auctionId': attr('$aid')}, 'ConsistentRead': True},
                            'AuctionExists', {'auction': '$exists($states.result.Item) ? $states.result.Item : {}'}),
        'AuctionExists': choice([('$exists($auction.auctionId.S)', 'SchemaReady')], 'AuctionNotExist'),
        'SchemaReady': choice([("$auction.schemaVersion.N = '2' and $exists($auction.version.N) and $exists($auction.highestBid.N) and $exists($auction.highestBidUserId.S)", 'AuctionOpen')], 'MigrationRequired'),
        'AuctionOpen': choice([("$auction.auctionStatus.S = 'OPEN'", 'CheckBid' if bidding else 'HasBid')], 'AuctionClosed'),
        'ReadUser': task('getItem', {'TableName': '${UsersTable}', 'Key': {'userId': attr('$uid')}, 'ConsistentRead': True},
                         'UserExists', {'user': '$exists($states.result.Item) ? $states.result.Item : {}'}),
        'UserExists': choice([('$exists($user.userId.S)', 'BalanceEnough')], 'InvalidUser'),
        'BalanceEnough': choice([('$number($user.acctBalance.N) >= $amount', 'PlanTransaction')], 'InsufficientFunds'),
        'WaitForConflict': {'Type': 'Wait', 'Seconds': 1, 'Next': 'ReadReceipt'},
        'ReturnSuccess': {'Type': 'Pass', 'End': True, 'Output': expr('$response')},
        'InvalidInput': reject('InvalidInput', 'Valid auctionId, userId/bidAmt (for bids), and optional requestId are required', 400),
        'RequestConflict': reject('IdempotencyConflict', 'requestId was already committed with a different payload', 409),
        'MigrationRequired': reject('MigrationRequired', 'Auction requires an offline schema migration before writes are allowed', 409),
        'AuctionNotExist': reject('AuctionNotExist', 'Auction does not exist'),
        'AuctionClosed': reject('AuctionClosed', 'Auction is already closed'),
        'InvalidUser': reject('invalid_user', 'User does not exist'),
        'InsufficientFunds': reject('insufficient_funds', 'Insufficient funds'),
        'Unavailable': reject('RetryableFailure', 'Outcome may be unknown. Retry the identical request with the same requestId.', 503, True),
    }
    if bidding:
        s['Initialize']['Assign'].update(uid=expr('$states.input.userId'), amount=expr('$states.input.bidAmt'))
        s['MakeIdentity']['Assign']['fingerprint'] = expr("$string({'auctionId':$aid,'userId':$uid,'bidAmt':$formatNumber($amount,'0.00')})")
        s['CheckBid'] = choice([('$amount > $number($auction.highestBid.N)', 'ReadUser')], 'InvalidBid')
        s['InvalidBid'] = reject('invalid_bid', 'Bid must strictly exceed the current highest bid')
        response = {'accepted': True, 'auctionId': expr('$aid'), 'userId': expr('$uid'), 'bidAmt': expr('$amount'),
                    'requestId': expr('$rid'), 'message': 'Bid accepted', 'notification': 'queued'}
    else:
        s['MakeIdentity']['Assign']['fingerprint'] = expr("$string({'auctionId':$aid})")
        s['SchemaReady']['Choices'][0]['Next'] = 'OwnerOnly'
        s['OwnerOnly'] = choice([("$principal = '' or $auction.ownerId.S = $principal", 'AuctionOpen')], 'Forbidden')
        s['Forbidden'] = reject('Forbidden', 'Only the auction creator can close this auction', 403)
        s['HasBid'] = choice([("$number($auction.highestBid.N) > 0 and $auction.highestBidUserId.S != ''", 'ReserveMet')], 'BidNotExist')
        s['ReserveMet'] = choice([('$number($auction.highestBid.N) >= $number($auction.reserve.N)', 'SelectWinner')], 'ReserveNotMet')
        s['SelectWinner'] = {'Type': 'Pass', 'Assign': {'amount': expr('$number($auction.highestBid.N)'), 'uid': expr('$auction.highestBidUserId.S')}, 'Next': 'ReadUser'}
        s['BidNotExist'] = reject('BidNotExist', 'No bids placed yet')
        s['ReserveNotMet'] = reject('ReserveNotMet', 'Reserve not met')
        response = {'accepted': True, 'auctionId': expr('$aid'), 'winningUserId': expr('$uid'), 'winningBid': expr('$amount'),
                    'requestId': expr('$rid'), 'message': 'Auction closed'}
    s['PlanTransaction'] = {'Type': 'Pass', 'Assign': {'response': response, 'token': expr('$uuid()')}, 'Next': 'Commit'}
    values = {':open': literal('OPEN'), ':version': attr('$auction.version.N', 'N'), ':one': literal(1, 'N'),
              ':amount': attr("$formatNumber($amount,'0.00')", 'N'), ':uid': attr('$uid')}
    update = {'TableName': '${AuctionsTable}', 'Key': {'auctionId': attr('$aid')}, 'ExpressionAttributeValues': values}
    if bidding:
        update.update(ConditionExpression='auctionStatus = :open AND version = :version AND highestBid < :amount',
                      UpdateExpression='SET version = version + :one, highestBid = :amount, highestBidUserId = :uid')
        user_action = {'ConditionCheck': {'TableName': '${UsersTable}', 'Key': {'userId': attr('$uid')},
                       'ConditionExpression': 'attribute_exists(userId) AND acctBalance >= :amount',
                       'ExpressionAttributeValues': {':amount': values[':amount']}}}
    else:
        values[':closed'] = literal('CLOSED')
        values[':result'] = attr('$string($response)')
        update.update(ConditionExpression='auctionStatus = :open AND version = :version AND highestBid = :amount AND highestBidUserId = :uid AND reserve <= :amount',
                      UpdateExpression='SET version = version + :one, auctionStatus = :closed, winningUserId = :uid, winningBid = :amount, closeResult = :result')
        user_action = {'Update': {'TableName': '${UsersTable}', 'Key': {'userId': attr('$uid')},
                       'ConditionExpression': 'attribute_exists(userId) AND acctBalance >= :amount',
                       'UpdateExpression': 'SET acctBalance = acctBalance - :amount',
                       'ExpressionAttributeValues': {':amount': values[':amount']}}}
    items = [{'Update': update}, user_action, {'Put': {'TableName': '${RequestsTable}',
             'ConditionExpression': 'attribute_not_exists(requestKey)', 'Item': {
                 'requestKey': attr('$requestKey'), 'fingerprint': attr('$fingerprint'),
                 'response': attr('$string($response)'), 'createdAt': attr('$createdAt')}}}]
    if bidding:
        event_payload = expr("$string({'eventId':$requestKey,'type':'NewHighestBid','auctionId':$aid,'userId':$uid,'bidAmt':$amount,'date':$createdAt})")
        s['PlanTransaction']['Assign']['eventPayload'] = event_payload
        items.extend([
            {'Put': {'TableName': '${BidsTable}', 'ConditionExpression': 'attribute_not_exists(auctionId)', 'Item': {
                'auctionId': attr('$aid'), 'bidAmt': values[':amount'], 'userId': attr('$uid'), 'date': attr('$createdAt'), 'requestId': attr('$rid')}}},
            {'Put': {'TableName': '${OutboxTable}', 'ConditionExpression': 'attribute_not_exists(eventId)', 'Item': {
                'eventId': attr('$requestKey'), 'payload': attr('$eventPayload'), 'deliveryStatus': literal('PENDING'), 'createdAt': attr('$createdAt')}}},
        ])
    s['Commit'] = {'Type': 'Task', 'Resource': 'arn:aws:states:::aws-sdk:dynamodb:transactWriteItems',
        'Arguments': {'ClientRequestToken': expr('$token'), 'TransactItems': items},
        'Next': 'PublishNotification' if bidding else 'ReturnSuccess',
        'Catch': [{'ErrorEquals': ['States.ALL'], 'Assign': {'attempt': expr('$attempt + 1')}, 'Next': 'WaitForConflict'}]}
    if bidding:
        s['PublishNotification'] = {'Type': 'Task', 'Resource': 'arn:aws:states:::sns:publish',
            'Arguments': {'TopicArn': '${HighBidTopicArn}', 'Message': expr('$eventPayload')}, 'Next': 'MarkSent',
            'Retry': [{'ErrorEquals': ['States.TaskFailed'], 'IntervalSeconds': 1, 'MaxAttempts': 2, 'BackoffRate': 2}],
            'Catch': [{'ErrorEquals': ['States.ALL'], 'Next': 'ReturnSuccess'}]}
        s['MarkSent'] = task('updateItem', {'TableName': '${OutboxTable}', 'Key': {'eventId': attr('$requestKey')},
            'UpdateExpression': 'SET deliveryStatus = :sent', 'ConditionExpression': 'deliveryStatus = :pending',
            'ExpressionAttributeValues': {':sent': literal('SENT'), ':pending': literal('PENDING')}}, 'ReturnSuccess', catch='ReturnSuccess')
    return {'Comment': 'Stage C: schema v2, conditional transactions, durable success receipts; generated by scripts/build_workflows.py. Deploy as EXPRESS.',
            'QueryLanguage': 'JSONata', 'StartAt': 'Validate', 'TimeoutSeconds': 25, 'States': s}


if __name__ == '__main__':
    for name, bidding in [('Bidding', True), ('CloseAuction', False)]:
        (ROOT / f'hw10/step_functions/{name}.json').write_text(json.dumps(generate(bidding), indent=2) + '\n')
