"""Operator-run cloud security test. Creates two disposable, email-suppressed identities.
Passwords and tokens exist only in memory; all test identities and keyed test data are removed.
"""
import argparse
import base64
import json
from pathlib import Path
import secrets
import time
import uuid
import requests
from release import config, connect, describe_stack, outputs, stack_name, save


def run(c, report):
    client,_=connect(c);out=outputs(describe_stack(client('cloudformation'),stack_name(c,'app')))
    cog=client('cognito-idp');db=client('dynamodb');pool=out['UserPoolId'];cid=out['UserPoolClientId']
    runid='auth-smoke-'+uuid.uuid4().hex[:12];aid=runid;created=[];subjects=[];extra_client=None;checks=[]
    def call(method,path,token=None,payload=None):
        headers={'Origin':out['WebUrl']}
        if token: headers['Authorization']=token
        response=requests.request(method,out['ApiBaseUrl']+path,headers=headers,json=payload,timeout=40)
        time.sleep(.25)
        return response.status_code,response.json() if response.text.strip() else {},response.headers
    def account(label,app_client):
        email=f'{runid}-{label}@example.com';password=secrets.token_urlsafe(32)+'aA1!'
        cog.admin_create_user(UserPoolId=pool,Username=email,TemporaryPassword=password,MessageAction='SUPPRESS',UserAttributes=[{'Name':'email','Value':email}]);created.append(email)
        cog.admin_set_user_password(UserPoolId=pool,Username=email,Password=password,Permanent=True)
        result=cog.admin_initiate_auth(UserPoolId=pool,ClientId=app_client,AuthFlow='ADMIN_USER_PASSWORD_AUTH',AuthParameters={'USERNAME':email,'PASSWORD':password})['AuthenticationResult']
        token=result['IdToken'];claims=json.loads(base64.urlsafe_b64decode(token.split('.')[1]+'=='))
        subjects.append(claims['sub']);return token,claims['sub'],email,password
    try:
        for method,path in [('GET','/user'),('POST','/user'),('GET','/auction'),('POST','/auction'),('PUT','/auction'),('GET','/auction/'+aid),('GET','/bid?auctionId='+aid),('POST','/bid')]:
            assert call(method,path,payload={} if method!='GET' else None)[0]==401,(method,path)
        assert call('OPTIONS','/user')[0]==200
        assert call('GET','/user','invalid-token')[0]==401
        checks.append('All eight business routes reject anonymous access; preflight succeeds')
        owner,owner_id,owner_email,owner_pw=account('owner',cid)
        bidder,bidder_id,_,_=account('bidder',cid)
        for token,uid in [(owner,owner_id),(bidder,bidder_id)]:
            status,data,_=call('POST','/user',token,{'userId':'victim','name':'Disposable test','acctBalance':999999})
            assert status==201 and data['userId']==uid and data['acctBalance']==1000,(status,data)
            assert call('POST','/user',token,{'name':'Cannot reset'})[0]==409
            data=call('GET','/user',token)[1];assert len(data)==1 and data[0]['userId']==uid
        checks.append('Profiles use token identity, fixed credits and owner-only reads; repeated creation cannot reset balance')
        status,data,_=call('POST','/auction',owner,{'auctionId':aid,'itemName':'Disposable authentication test','reserve':1,'ownerId':bidder_id})
        assert status==201 and data['ownerId']==owner_id,(status,data)
        payload={'auctionId':aid,'userId':owner_id,'_principal':owner_id,'bidAmt':10,'requestId':runid+'-bid'}
        status,bid,_=call('POST','/bid',bidder,payload)
        assert status==200 and bid.get('accepted') and bid['userId']==bidder_id,(status,bid)
        assert call('POST','/bid',bidder,payload)[1]==bid
        assert call('POST','/bid',bidder,{**payload,'bidAmt':11})[0]==409
        status,denied,_=call('PUT','/auction',bidder,{'auctionId':aid,'requestId':runid+'-close','_principal':owner_id})
        assert status==403 and denied['reason']=='Forbidden',(status,denied)
        status,closed,_=call('PUT','/auction',owner,{'auctionId':aid,'requestId':runid+'-close'})
        assert status==200 and closed.get('accepted'),(status,closed)
        assert call('PUT','/auction',owner,{'auctionId':aid,'requestId':runid+'-close'})[1]==closed
        assert call('PUT','/auction',bidder,{'auctionId':aid,'requestId':runid+'-close'})[0]==403
        assert call('GET','/user',bidder)[1][0]['acctBalance']==990
        assert call('GET','/auction/'+aid,owner)[1]['auctionStatus']=='CLOSED'
        assert call('GET','/bid?auctionId='+aid,owner)[1][0]['userId']==bidder_id
        checks.append('Forged bidder/owner fields ignored; owner-only close; retries charge winner once; cross-user receipt replay denied')
        extra_client=cog.create_user_pool_client(UserPoolId=pool,ClientName=runid,GenerateSecret=False,ExplicitAuthFlows=['ALLOW_ADMIN_USER_PASSWORD_AUTH','ALLOW_REFRESH_TOKEN_AUTH'])['UserPoolClient']['ClientId']
        foreign=cog.admin_initiate_auth(UserPoolId=pool,ClientId=extra_client,AuthFlow='ADMIN_USER_PASSWORD_AUTH',AuthParameters={'USERNAME':owner_email,'PASSWORD':owner_pw})['AuthenticationResult']['IdToken']
        assert call('GET','/user',foreign)[0]==401
        checks.append('Valid token from another Cognito app client is rejected')
        save(report,{'status':'passed','run_id':runid,'checks':checks,'web_url':out['WebUrl']})
        print(json.dumps({'status':'passed','checks':checks},indent=2))
    finally:
        if extra_client:cog.delete_user_pool_client(UserPoolId=pool,ClientId=extra_client)
        for email in created:cog.admin_delete_user(UserPoolId=pool,Username=email)
        for uid in subjects:
            db.delete_item(TableName=out['UsersTable'],Key={'userId':{'S':uid}})
            for kind,suffix in [('BID','bid'),('CLOSE','close')]:
                key=f'{kind}#{uid}#{runid}-{suffix}'
                db.delete_item(TableName=out['RequestsTable'],Key={'requestKey':{'S':key}})
                if kind=='BID':db.delete_item(TableName=out['OutboxTable'],Key={'eventId':{'S':key}})
        db.delete_item(TableName=out['AuctionsTable'],Key={'auctionId':{'S':aid}})
        db.delete_item(TableName=out['BidsTable'],Key={'auctionId':{'S':aid},'bidAmt':{'N':'10'}})

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,default=Path('infra/deploy.local.json'));p.add_argument('--out',type=Path,default=Path('.local/auth-smoke.json'))
    a=p.parse_args();run(config(a.config),a.out)
