from auction_common import all_pages, http_handler, principal, table


@http_handler
def lambda_handler(event, context):
    uid = principal(event)
    if uid:
        item = table('USERS_TABLE').get_item(Key={'userId': uid}, ConsistentRead=True).get('Item')
        return 200, [item] if item else []
    return 200, all_pages(table('USERS_TABLE'), 'scan', ConsistentRead=True)
