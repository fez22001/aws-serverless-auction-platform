"""Public-demo security boundaries, exercised against the actual handlers/workflows."""
from decimal import Decimal
from test_workflows import invoke, evaluator, balance
from asl_runner import Execution
from conftest import load_lambda


def context(uid):
    return {'requestContext': {'authorizer': {'claims': {'sub': uid}}}}


def test_deployed_handlers_fail_closed_without_claims(aws, monkeypatch):
    monkeypatch.setenv('REQUIRE_AUTH', 'true')
    for name in ['user_post_create','user_get_all','auction_post_create','auction_get_all','auction_get_one','bid_get_for_auction']:
        assert load_lambda(name).lambda_handler({},None)['statusCode']==401


def test_profile_identity_balance_and_reads_cannot_be_forged(aws, monkeypatch):
    monkeypatch.setenv('REQUIRE_AUTH','true')
    status, row=invoke('user_post_create', {'userId':'victim','name':'Alice','acctBalance':999999}, **context('alice'))
    assert status==201 and row=={'userId':'alice','name':'Alice','acctBalance':1000}
    assert invoke('user_post_create', {'name':'Again','acctBalance':999999}, **context('alice'))[0]==409
    assert invoke('user_get_all', **context('bob'))[1]==[]
    assert invoke('user_get_all', **context('alice'))[1]==[row]
    auction=invoke('auction_post_create', {'auctionId':'a','itemName':'Camera','reserve':1,'ownerId':'victim'}, **context('alice'))[1]
    assert auction['ownerId']=='alice'


def test_only_owner_closes_and_receipts_are_scoped_to_principal(aws, evaluator, monkeypatch):
    monkeypatch.setenv('REQUIRE_AUTH','true')
    for uid in ['alice','bob']:
        invoke('user_post_create', {'name':uid}, **context(uid))
    invoke('auction_post_create', {'auctionId':'camera','itemName':'Camera','reserve':1}, **context('alice'))
    def run(kind, uid, **fields):
        return Execution(kind, {'auctionId':'camera','requestId':'shared-id','_principal':uid, **fields}, aws, evaluator).run()
    assert run('Bidding','bob',userId='bob',bidAmt=10)['accepted']
    assert run('CloseAuction','bob')['httpStatus']==403
    assert balance(aws,'bob')==1000
    good=run('CloseAuction','alice')
    assert good['accepted'] and balance(aws,'bob')==990
    assert run('CloseAuction','bob')['httpStatus']==403  # Cannot replay the owner's receipt.
    assert run('CloseAuction','alice')==good and balance(aws,'bob')==990
