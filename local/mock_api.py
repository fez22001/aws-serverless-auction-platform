"""Loopback-only, in-memory auction demo. No AWS imports or outbound requests.

The lock and event log simulate local semantics only. They do not execute the
historical Lambda/JSONata code or prove DynamoDB/SNS/SQS correctness.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from threading import RLock
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

ORIGINS = {f'http://{host}:{port}' for host in ('127.0.0.1', 'localhost') for port in (5173, 4173)}


class APIError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def identifier(value, field):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value):
        raise APIError(400, f'{field} must contain 1–64 letters, numbers, underscores or hyphens')
    return value


def text_field(body, field, limit=200):
    value = body.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise APIError(400, f'{field} is required (maximum {limit} characters)')
    return value.strip()


def money(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise APIError(400, f'{field} must be a number')
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 <= amount <= 1_000_000_000 or amount != amount.quantize(Decimal('.01')):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise APIError(400, f'{field} must be between 0 and 1000000000 with at most two decimal places') from None
    return amount


def json_value(value):
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(type(value).__name__)


class Store:
    def __init__(self, seed=True):
        self.users = {}
        self.auctions = {}
        self.bids = {}
        self.events = []
        self.receipts = {}
        self.lock = RLock()
        if seed:
            self.request('POST', '/user', {}, {'userId': 'demo-alice', 'name': 'Alice (synthetic)', 'acctBalance': 500})
            self.request('POST', '/user', {}, {'userId': 'demo-bob', 'name': 'Bob (synthetic)', 'acctBalance': 150})
            self.request('POST', '/auction', {}, {'auctionId': 'demo-camera', 'itemName': 'Demo Camera', 'reserve': 100, 'description': 'Synthetic demo item — no real sale.'})

    def request(self, method, path, query=None, body=None):
        with self.lock:
            data = body or {}
            key = fingerprint = None
            if method in ('POST', 'PUT') and path in ('/bid', '/auction') and data.get('requestId') is not None:
                rid = data['requestId']
                if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', rid):
                    raise APIError(400, 'requestId must contain 1–128 letters, numbers, underscores or hyphens')
                if (method, path) in (('POST', '/bid'), ('PUT', '/auction')):
                    key = (method, path, rid)
                    fingerprint = (identifier(data.get('auctionId'), 'auctionId'),)
                    if path == '/bid':
                        fingerprint += (identifier(data.get('userId'), 'userId'), money(data.get('bidAmt'), 'bidAmt'))
                    if key in self.receipts:
                        old_fingerprint, result = self.receipts[key]
                        if old_fingerprint != fingerprint:
                            raise APIError(409, 'requestId was already committed with a different payload')
                        return deepcopy(result)
            result = self._request(method, path, query or {}, data)
            if key and result[1].get('accepted'):
                self.receipts[key] = (fingerprint, deepcopy(result))
            return deepcopy(result)

    def _request(self, method, path, query, body):
        if method == 'GET' and path == '/__mock/health':
            return 200, {'mode': 'local-mock', 'synthetic': True, 'persistent': False}
        if method == 'GET' and path == '/__mock/events':
            return 200, self.events
        if path == '/user':
            if method == 'GET':
                return 200, list(self.users.values())
            if method == 'POST':
                uid = identifier(body.get('userId') or str(uuid4()), 'userId')
                name = text_field(body, 'name')
                balance = money(body.get('acctBalance'), 'acctBalance')
                if uid in self.users:
                    raise APIError(409, 'User ID already exists')
                self.users[uid] = {'userId': uid, 'name': name, 'acctBalance': balance}
                return 201, self.users[uid]
        if path == '/auction':
            if method == 'GET':
                return 200, list(self.auctions.values())
            if method == 'POST':
                aid = identifier(body.get('auctionId') or str(uuid4()), 'auctionId')
                name = text_field(body, 'itemName')
                reserve = money(body.get('reserve'), 'reserve')
                description = body.get('description', '')
                if not isinstance(description, str) or len(description) > 2000:
                    raise APIError(400, 'description must be a string of at most 2000 characters')
                if aid in self.auctions:
                    raise APIError(409, 'Auction ID already exists')
                self.auctions[aid] = {'auctionId': aid, 'itemName': name, 'reserve': reserve,
                                      'description': description, 'auctionStatus': 'OPEN', 'winningUserId': ''}
                self.bids[aid] = []
                return 201, self.auctions[aid]
            if method == 'PUT':
                return self.close(identifier(body.get('auctionId'), 'auctionId'))
        if method == 'GET' and path.startswith('/auction/'):
            aid = identifier(unquote(path[len('/auction/'):]), 'auctionId')
            if aid not in self.auctions:
                raise APIError(404, 'Auction not found')
            return 200, self.auctions[aid]
        if path == '/bid':
            if method == 'GET':
                aid = identifier((query.get('auctionId') or query.get('auctionid') or [None])[0], 'auctionId')
                if aid not in self.auctions:
                    raise APIError(404, 'Auction not found')
                return 200, sorted(self.bids[aid], key=lambda b: b['bidAmt'], reverse=True)
            if method == 'POST':
                aid = identifier(body.get('auctionId'), 'auctionId')
                uid = identifier(body.get('userId'), 'userId')
                amount = money(body.get('bidAmt'), 'bidAmt')
                if aid not in self.auctions:
                    return self.reject('AuctionNotExist', 'Auction does not exist')
                if self.auctions[aid]['auctionStatus'] != 'OPEN':
                    return self.reject('AuctionClosed', 'Auction is already closed')
                if uid not in self.users:
                    return self.reject('invalid_user', 'User does not exist')
                if amount > self.users[uid]['acctBalance']:
                    return self.reject('insufficient_funds', 'Insufficient funds')
                highest = max((b['bidAmt'] for b in self.bids[aid]), default=Decimal(0))
                if amount <= highest:
                    return self.reject('invalid_bid', 'Bid must be strictly greater than the current highest bid')
                bid = {'auctionId': aid, 'userId': uid, 'bidAmt': amount, 'date': datetime.now(timezone.utc).isoformat()}
                self.bids[aid].append(bid)
                self.events.append({'eventId': str(uuid4()), 'type': 'NewHighestBid', 'synthetic': True, 'delivery': 'local-log-only', **bid})
                return 200, {'accepted': True, 'message': 'Bid accepted', **bid}
        raise APIError(404, 'Route not found')

    @staticmethod
    def reject(reason, message):
        return 200, {'accepted': False, 'reason': reason, 'message': message}

    def close(self, aid):
        auction = self.auctions.get(aid)
        if auction is None:
            return self.reject('AuctionNotExist', 'Auction does not exist')
        if auction['auctionStatus'] != 'OPEN':
            return self.reject('AuctionClosed', 'Auction is already closed')
        if not self.bids[aid]:
            return self.reject('BidNotExist', 'No bids placed yet')
        top = max(self.bids[aid], key=lambda b: b['bidAmt'])
        if top['bidAmt'] < auction['reserve']:
            return self.reject('ReserveNotMet', 'Reserve not met')
        user = self.users[top['userId']]
        if user['acctBalance'] < top['bidAmt']:
            return self.reject('InsufficientFunds', 'Winner has insufficient funds')
        user['acctBalance'] -= top['bidAmt']
        auction.update(auctionStatus='CLOSED', winningUserId=top['userId'])
        return 200, {'accepted': True, 'message': 'Auction closed', 'auctionId': aid,
                     'winningUserId': top['userId'], 'winningBid': top['bidAmt']}


def make_server(port=8000, seed=True):
    store = Store(seed)

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, data):
            encoded = json.dumps(data, default=json_value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            origin = self.headers.get('Origin')
            if origin in ORIGINS:
                self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(encoded)

        def handle_api(self):
            try:
                if self.headers.get('Origin') and self.headers['Origin'] not in ORIGINS:
                    raise APIError(403, 'Origin is not a local demo origin')
                if self.command == 'OPTIONS':
                    return self.respond(200, {})
                body = {}
                if self.command in ('POST', 'PUT'):
                    if self.headers.get_content_type() != 'application/json':
                        raise APIError(415, 'Content-Type must be application/json')
                    try:
                        size = int(self.headers.get('Content-Length', '0'))
                        if size <= 0 or size > 100_000:
                            raise ValueError
                        body = json.loads(self.rfile.read(size), parse_float=Decimal,
                                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                        if not isinstance(body, dict):
                            raise ValueError
                    except (ValueError, UnicodeDecodeError):
                        raise APIError(400, 'Request must be a JSON object of at most 100000 bytes') from None
                url = urlsplit(self.path)
                status, data = store.request(self.command, url.path, parse_qs(url.query), body)
                self.respond(status, data)
            except APIError as exc:
                self.respond(exc.status, {'error': exc.message, 'message': exc.message})

        do_GET = do_POST = do_PUT = do_OPTIONS = handle_api

        def log_message(self, fmt, *args):
            print('mock:', fmt % args, flush=True)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--empty', action='store_true', help='Start without synthetic seed records')
    args = parser.parse_args()
    server = make_server(args.port, seed=not args.empty)
    print(f'Synthetic local mock: http://127.0.0.1:{server.server_port}; data resets on restart; no AWS calls.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
