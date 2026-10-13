"""Shared HTTP validation; bundled alongside each Lambda handler."""
import base64
from decimal import Decimal, InvalidOperation
from functools import wraps
import json
import logging
import os
import re
from uuid import uuid4

import boto3
from botocore.exceptions import BotoCoreError, ClientError

LOG = logging.getLogger(__name__)


class AuthenticationRequired(Exception):
    pass


def principal(event):
    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
    if os.environ.get("REQUIRE_AUTH") == "true":
        if not claims.get("sub"):
            raise AuthenticationRequired()
        return identifier(claims["sub"], "principal")
    return None


class InvalidInput(ValueError):
    pass


def identifier(value, field, optional=False):
    if optional and value in (None, ''):
        return str(uuid4())
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value):
        raise InvalidInput(f'{field} must contain 1–64 letters, numbers, underscores or hyphens')
    return value


def text(body, field, maximum=200):
    value = body.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise InvalidInput(f'{field} is required (maximum {maximum} characters)')
    return value.strip()


def money(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise InvalidInput(f'{field} must be a JSON number')
    try:
        amount = Decimal(value)
        if not amount.is_finite() or not 0 <= amount <= 1_000_000_000 or amount != amount.quantize(Decimal('.01')):
            raise InvalidOperation
    except InvalidOperation:
        raise InvalidInput(f'{field} must be nonnegative, at most 1000000000, and have at most two decimal places') from None
    return amount


def reject_constant(_):
    raise InvalidInput('Non-finite numbers are not allowed')


def body(event):
    try:
        raw = event.get('body') or '{}'
        if event.get('isBase64Encoded'):
            raw = base64.b64decode(raw, validate=True).decode('utf-8')
        result = json.loads(raw, parse_float=Decimal, parse_constant=reject_constant)
        if not isinstance(result, dict):
            raise InvalidInput('Body must be a JSON object')
        return result
    except (ValueError, TypeError, UnicodeError) as exc:
        raise InvalidInput('Body must be a valid JSON object with finite numbers') from exc


def table(env):
    # Lazy initialization keeps imports independent of AWS identity or network.
    return boto3.resource('dynamodb').Table(os.environ[env])


def native(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral() else float(value)
    raise TypeError(type(value).__name__)


def response(status, data):
    return {'statusCode': status, 'headers': {'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': os.environ.get('ALLOWED_ORIGIN', 'http://127.0.0.1:5173')},
            'body': json.dumps(data, default=native, allow_nan=False)}


def http_handler(function):
    @wraps(function)
    def handler(event, context):
        try:
            principal(event)  # Fail closed in every deployed HTTP Lambda.
            status, data = function(event, context)
            return response(status, data)
        except AuthenticationRequired:
            return response(401, {"message": "Sign in required"})
        except InvalidInput as exc:
            return response(400, {'error': str(exc), 'message': str(exc)})
        except ClientError as exc:
            if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
                return response(409, {'error': 'ID already exists', 'message': 'ID already exists'})
            LOG.exception('AWS operation failed')
            return response(503, {'error': 'Service unavailable', 'message': 'Service unavailable; retry with the same ID'})
        except BotoCoreError:
            LOG.exception('AWS connection failed')
            return response(503, {'error': 'Service unavailable', 'message': 'Service unavailable; retry with the same ID'})
    return handler


def all_pages(tbl, operation, **kwargs):
    items = []
    while True:
        page = getattr(tbl, operation)(**kwargs)
        items.extend(page.get('Items', []))
        if not page.get('LastEvaluatedKey'):
            return items
        kwargs['ExclusiveStartKey'] = page['LastEvaluatedKey']
