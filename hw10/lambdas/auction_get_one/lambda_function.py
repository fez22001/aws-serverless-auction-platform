from auction_common import http_handler, identifier, table


@http_handler
def lambda_handler(event, context):
    aid = identifier((event.get('pathParameters') or {}).get('auctionId'), 'auctionId')
    item = table('AUCTIONS_TABLE').get_item(Key={'auctionId': aid}, ConsistentRead=True).get('Item')
    return (200, item) if item else (404, {'error': 'Auction not found'})
