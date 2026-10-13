import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from scripts.build_workflows import generate

ROOT=Path(__file__).resolve().parents[2]
NODE=os.environ.get('NODE_BIN') or shutil.which('node') or str(Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node')


def render(folder,name,body):
    request={'path':str(ROOT/f'hw10/apigw/{folder}/{name}.vtl'),'body':body}
    output=subprocess.check_output([NODE,str(ROOT/'local/asl/render_vtl.cjs')],input=json.dumps(request),text=True)
    value=json.loads(output)
    return value['status'],json.loads(value['rendered'])


@pytest.mark.parametrize('name', ['bidPOST','auctionPUT'])
def test_vtl_discards_untrusted_identity_and_preserves_json(name):
    payload={'auctionId': "O'Brien\\x\"路径", 'requestId':'same-request', 'userId':'victim', '_principal':'victim', 'bidAmt':12.25}
    status,mapped=render('request_mappings',name,json.dumps(payload,ensure_ascii=False))
    assert status==200
    actual=json.loads(mapped['input'])
    assert actual['auctionId']==payload['auctionId']
    assert actual['_principal']=='signed-in-user'
    assert actual['requestId']=='same-request'
    if name=='bidPOST':
        assert actual['userId']=='signed-in-user' and actual['bidAmt']==12.25
    else:
        assert 'userId' not in actual


@pytest.mark.parametrize('name', ['bidPOST','auctionPUT'])
@pytest.mark.parametrize('body,expected', [({'accepted':True,'message':"O'Brien"},200),
    ({'accepted':False,'reason':'invalid_bid'},200),
    ({'accepted':False,'httpStatus':409,'reason':'IdempotencyConflict'},409),
    ({'accepted':False,'httpStatus':400,'reason':'InvalidInput'},400),
    ({'accepted':False,'httpStatus':503,'retryable':True},503)])
def test_vtl_success_is_json_not_a_java_map(name,body,expected):
    status,rendered=render('response_mappings',name,json.dumps({'status':'SUCCEEDED','output':json.dumps(body)}))
    assert status==expected and rendered==body


@pytest.mark.parametrize('name', ['bidPOST','auctionPUT'])
def test_vtl_failed_execution_does_not_expose_cause(name):
    status,body=render('response_mappings',name,json.dumps({'status':'FAILED','cause':'private internal details'}))
    assert status==503 and body['retryable']
    assert 'private' not in json.dumps(body)


def test_generated_definitions_have_valid_targets_and_no_old_bindings():
    for name,bidding in [('Bidding',True),('CloseAuction',False)]:
        source=(ROOT/f'hw10/step_functions/{name}.json').read_text()
        definition=json.loads(source)
        assert definition==generate(bidding)
        states=definition['States']
        assert definition['StartAt'] in states
        for state in states.values():
            targets=[state.get('Next'),state.get('Default')]
            targets += [x.get('Next') for x in state.get('Choices',[])+state.get('Catch',[])]
            assert all(target is None or target in states for target in targets)
        assert '211125456045' not in source and 'HW10-' not in source
        assert 'transactWriteItems' in source
        assert '$eval(' not in source


def test_browser_retry_identity_survives_module_reload_and_storage_failure():
    module=(ROOT/'ReactAuction/src/requestIdentity.ts').as_uri()
    script=f"""
import assert from 'node:assert/strict';
const storage=new Map();
globalThis.sessionStorage={{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)}};
const first=await import('{module}?first');
const payload={{auctionId:'a',userId:'u',bidAmt:12.25}};
const id=first.requestIdentity('bid',payload);
assert.equal(first.requestIdentity('bid',payload),id);
const reloaded=await import('{module}?reloaded');
assert.equal(reloaded.requestIdentity('bid',payload),id);
assert.notEqual(reloaded.requestIdentity('bid',{{...payload,bidAmt:13}}),id);
assert.notEqual(reloaded.requestIdentity('close',{{auctionId:'a'}}),id);
globalThis.sessionStorage={{getItem:()=>{{throw Error('blocked')}},setItem:()=>{{throw Error('blocked')}}}};
const fallback=reloaded.requestIdentity('bid',{{...payload,auctionId:'b'}});
assert.equal(reloaded.requestIdentity('bid',{{...payload,auctionId:'b'}}),fallback);
console.log('identity assertions passed');
"""
    subprocess.run([NODE,'--input-type=module','-e',script],check=True,capture_output=True,text=True)
