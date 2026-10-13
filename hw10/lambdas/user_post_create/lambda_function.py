from auction_common import body, principal, http_handler, identifier, money, table, text


@http_handler
def lambda_handler(event, context):
    data = body(event)
    uid = principal(event)
    if uid:
        data = {'userId': uid, 'name': data.get('name'), 'acctBalance': 1000}
    item = {'userId': identifier(data.get('userId'), 'userId', optional=True),
            'name': text(data, 'name'), 'acctBalance': money(data.get('acctBalance'), 'acctBalance')}
    table('USERS_TABLE').put_item(Item=item, ConditionExpression='attribute_not_exists(userId)')
    return 201, item
