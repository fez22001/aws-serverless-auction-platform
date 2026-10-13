"""Local mock acceptance tests only; these do not execute AWS code."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import socket
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from local.mock_api import APIError, Store, make_server


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        assert address[0] in ('127.0.0.1', '::1'), f'External connection blocked: {address[0]}'
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)


@pytest.fixture
def store():
    return Store()


def bid(store, amount=120, user='demo-alice', auction='demo-camera'):
    return store.request('POST', '/bid', body={'auctionId': auction, 'userId': user, 'bidAmt': amount})[1]


def close(store, auction='demo-camera'):
    return store.request('PUT', '/auction', body={'auctionId': auction})[1]


def test_full_lifecycle_and_no_double_debit(store):
    assert bid(store)['accepted']
    assert close(store)['winningUserId'] == 'demo-alice'
    assert store.users['demo-alice']['acctBalance'] == 380
    assert store.auctions['demo-camera']['auctionStatus'] == 'CLOSED'
    assert not close(store)['accepted']
    assert not bid(store, 130)['accepted']
    assert store.users['demo-alice']['acctBalance'] == 380
    assert len(store.events) == 1
    assert store.events[0]['delivery'] == 'local-log-only'


@pytest.mark.parametrize('amount,user,auction,reason', [
    (10, 'missing', 'demo-camera', 'invalid_user'),
    (10, 'demo-alice', 'missing', 'AuctionNotExist'),
    (501, 'demo-alice', 'demo-camera', 'insufficient_funds'),
    (0, 'demo-alice', 'demo-camera', 'invalid_bid'),
])
def test_rejected_bids_do_not_mutate(store, amount, user, auction, reason):
    assert bid(store, amount, user, auction)['reason'] == reason
    assert store.events == []
    assert store.bids['demo-camera'] == []


def test_reserve_and_strict_increase(store):
    assert close(store)['reason'] == 'BidNotExist'
    assert bid(store, 90)['accepted']
    assert close(store)['reason'] == 'ReserveNotMet'
    assert not bid(store, 90)['accepted']
    assert not bid(store, 89)['accepted']
    assert store.users['demo-alice']['acctBalance'] == 500
    assert bid(store, 100)['accepted']
    assert close(store)['accepted']


@pytest.mark.parametrize('amount', [True, None, '12', -1, 0.001, float('nan'), float('inf'), 1000000001])
def test_invalid_money(store, amount):
    with pytest.raises(APIError) as exc:
        bid(store, amount)
    assert exc.value.status == 400
    assert store.events == []


def test_cents_preserved(store):
    assert bid(store, Decimal('100.25'))['accepted']
    assert close(store)['accepted']
    assert store.users['demo-alice']['acctBalance'] == Decimal('399.75')


def test_duplicate_creation_cannot_reset_or_reopen(store):
    bid(store); close(store)
    for path, body in [('/user', {'userId': 'demo-alice', 'name': 'reset', 'acctBalance': 999}),
                       ('/auction', {'auctionId': 'demo-camera', 'itemName': 'reset', 'reserve': 0})]:
        with pytest.raises(APIError) as exc:
            store.request('POST', path, body=body)
        assert exc.value.status == 409
    assert store.users['demo-alice']['acctBalance'] == 380
    assert store.auctions['demo-camera']['auctionStatus'] == 'CLOSED'


def test_simultaneous_equal_bids_and_closes(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: bid(store), range(16)))
    assert sum(r['accepted'] for r in results) == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: close(store), range(16)))
    assert sum(r['accepted'] for r in results) == 1
    assert store.users['demo-alice']['acctBalance'] == 380


def test_balance_rechecked_at_close(store):
    store.request('POST', '/auction', body={'auctionId': 'second', 'itemName': 'Second', 'reserve': 1})
    assert bid(store, 400)['accepted']
    assert bid(store, 400, auction='second')['accepted']
    assert close(store)['accepted']
    assert close(store, 'second')['reason'] == 'InsufficientFunds'
    assert store.auctions['second']['auctionStatus'] == 'OPEN'


@pytest.fixture
def http_api():
    server = make_server(0, seed=False)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(method, path, body=None, origin='http://127.0.0.1:5173', raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = Request(f'http://127.0.0.1:{server.server_port}{path}', data=data, method=method,
                      headers={'Content-Type': 'application/json', 'Origin': origin})
        try:
            response = urlopen(req, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response), response.headers
    yield request
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_all_eight_routes_and_cors(http_api):
    call = http_api
    assert call('OPTIONS', '/bid')[2]['Access-Control-Allow-Origin'] == 'http://127.0.0.1:5173'
    assert call('POST', '/user', {'userId': 'u', 'name': "O'Brien", 'acctBalance': 200.50})[0] == 201
    assert call('GET', '/user')[1][0]['name'] == "O'Brien"
    assert call('POST', '/auction', {'auctionId': 'a', 'itemName': 'Item', 'reserve': 50})[0] == 201
    assert call('GET', '/auction')[1][0]['auctionStatus'] == 'OPEN'
    assert call('GET', '/auction/a')[1]['auctionId'] == 'a'
    assert call('POST', '/bid', {'auctionId': 'a', 'userId': 'u', 'bidAmt': 50.25})[1]['accepted']
    assert call('GET', '/bid?auctionId=a')[1][0]['bidAmt'] == 50.25
    assert call('PUT', '/auction', {'auctionId': 'a'})[1]['accepted']
    assert call('GET', '/user')[1][0]['acctBalance'] == 150.25
    assert call('GET', '/__mock/events')[1][0]['synthetic']


@pytest.mark.parametrize('raw', [b'{', b'[]', b'null', b'{"acctBalance":NaN}'])
def test_malformed_http_body(http_api, raw):
    assert http_api('POST', '/user', raw=raw)[0] == 400


def test_http_errors_and_origin(http_api):
    assert http_api('GET', '/auction/missing')[0] == 404
    assert http_api('GET', '/bid')[0] == 400
    assert http_api('GET', '/__mock/health', origin='https://unrelated.example')[0] == 403


def test_mock_request_id_replay(store):
    body={'auctionId':'demo-camera','userId':'demo-alice','bidAmt':120.25,'requestId':'stable'}
    first=store.request('POST','/bid',body=body)
    assert store.request('POST','/bid',body=body)==first
    assert len(store.events)==1
    with pytest.raises(APIError) as exc:
        store.request('POST','/bid',body={**body,'bidAmt':130})
    assert exc.value.status==409
    closed=store.request('PUT','/auction',body={'auctionId':'demo-camera','requestId':'close-stable'})
    assert store.request('PUT','/auction',body={'auctionId':'demo-camera','requestId':'close-stable'})==closed
    assert store.users['demo-alice']['acctBalance']==Decimal('379.75')
