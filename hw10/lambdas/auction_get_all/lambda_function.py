from auction_common import all_pages, http_handler, table


@http_handler
def lambda_handler(event, context):
    return 200, all_pages(table('AUCTIONS_TABLE'), 'scan', ConsistentRead=True)
