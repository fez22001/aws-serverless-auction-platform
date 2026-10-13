from decimal import Decimal
from auction_common import InvalidInput, body, principal, http_handler, identifier, money, table, text


@http_handler
def lambda_handler(event, context):
    data = body(event)
    description = data.get('description', '')
    if not isinstance(description, str) or len(description) > 2000:
        raise InvalidInput('description must be a string of at most 2000 characters')
    item = {'auctionId': identifier(data.get('auctionId'), 'auctionId', optional=True),
            'itemName': text(data, 'itemName'), 'reserve': money(data.get('reserve'), 'reserve'),
            'description': description, 'auctionStatus': 'OPEN', 'winningUserId': '',
            'schemaVersion': 2, 'version': 0, 'highestBid': Decimal(0), 'highestBidUserId': ''}
    uid = principal(event)
    if uid:
        item['ownerId'] = uid
    table('AUCTIONS_TABLE').put_item(Item=item, ConditionExpression='attribute_not_exists(auctionId)')
    return 201, item
