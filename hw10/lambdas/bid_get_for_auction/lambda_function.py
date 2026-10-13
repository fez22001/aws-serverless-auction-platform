from boto3.dynamodb.conditions import Key
from auction_common import all_pages, http_handler, identifier, table


@http_handler
def lambda_handler(event, context):
    query = event.get('queryStringParameters') or {}
    aid = identifier(query.get('auctionId') or query.get('auctionid'), 'auctionId')
    if not table('AUCTIONS_TABLE').get_item(Key={'auctionId': aid}, ConsistentRead=True).get('Item'):
        return 404, {'error': 'Auction not found'}
    return 200, all_pages(table('BIDS_TABLE'), 'query', KeyConditionExpression=Key('auctionId').eq(aid),
                          ScanIndexForward=False, ConsistentRead=True)
