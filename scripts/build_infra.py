"""Generate native CloudFormation from the reviewed workflow/VTL sources; offline only."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
def ref(name): return {'Ref': name}
def sub(value): return {'Fn::Sub': value}
def att(name, key='Arn'): return {'Fn::GetAtt': [name, key]}
def policy(*statements): return {'Version': '2012-10-17', 'Statement': list(statements)}
def allow(actions, resources, **extra):
    return {'Effect': 'Allow', 'Action': actions, 'Resource': resources, **extra}
def trust(service):
    return policy({'Effect': 'Allow', 'Principal': {'Service': service}, 'Action': 'sts:AssumeRole'})
def resource(kind, **props): return {'Type': 'AWS::'+kind, 'Properties': props}
def param(default=None, **kw):
    return {'Type': 'String', **({'Default': default} if default is not None else {}), **kw}
def base(description):
    return {'AWSTemplateFormatVersion': '2010-09-09', 'Description': description,
            'Parameters': {'ProjectPrefix': param('auction-dev', AllowedPattern='[a-z][a-z0-9-]{2,24}')}, 'Resources': {}, 'Outputs': {}}
def output(t, name, value): t['Outputs'][name] = {'Value': value}
def bucket(name=None):
    return resource('S3::Bucket', **({'BucketName': name} if name else {}),
        PublicAccessBlockConfiguration={k: True for k in ['BlockPublicAcls','BlockPublicPolicy','IgnorePublicAcls','RestrictPublicBuckets']},
        OwnershipControls={'Rules':[{'ObjectOwnership':'BucketOwnerEnforced'}]},
        BucketEncryption={'ServerSideEncryptionConfiguration':[{'ServerSideEncryptionByDefault':{'SSEAlgorithm':'AES256'}}]},
        VersioningConfiguration={'Status':'Enabled'})
def tls_deny(bucket_id):
    return {'Effect':'Deny','Principal':'*','Action':'s3:*','Resource':[att(bucket_id), sub('${'+bucket_id+'.Arn}/*')],
            'Condition':{'Bool':{'aws:SecureTransport':'false'}}}
def retained(r): return {**r, 'DeletionPolicy':'Retain', 'UpdateReplacePolicy':'Retain'}
LOG_DELIVERY = ['logs:CreateLogDelivery','logs:GetLogDelivery','logs:UpdateLogDelivery','logs:DeleteLogDelivery',
                'logs:ListLogDeliveries','logs:PutResourcePolicy','logs:DescribeResourcePolicies','logs:DescribeLogGroups']


def bootstrap():
    t=base('Auction recovery: operator-managed deployment identity and artifact storage. No application data.')
    p=t['Parameters']; r=t['Resources']
    p.update({'GitHubRepository':param('', AllowedPattern='(^$|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$)'),
              'GitHubEnvironment':param('dev', AllowedPattern='[A-Za-z0-9_-]+'),
              'ExistingOidcProviderArn':param(''),
              'BudgetEmail':param(''), 'MonthlyBudgetUsd':{'Type':'Number','Default':10,'MinValue':1},
              'ManageApiGatewayLogging':param('false', AllowedValues=['true','false'])})
    t['Conditions']={'UseGitHub':{'Fn::Not':[{'Fn::Equals':[ref('GitHubRepository'),'']}]},
        'CreateOidc':{'Fn::And':[{'Condition':'UseGitHub'},{'Fn::Equals':[ref('ExistingOidcProviderArn'),'']}]},
        'ManageLogging':{'Fn::Equals':[ref('ManageApiGatewayLogging'),'true']},
        'BudgetEnabled':{'Fn::Not':[{'Fn::Equals':[ref('BudgetEmail'),'']}]}}
    r['AccountBudget']={**resource('Budgets::Budget',Budget={'BudgetName':sub('${ProjectPrefix}-monthly-account'),'BudgetType':'COST','TimeUnit':'MONTHLY',
        'BudgetLimit':{'Amount':ref('MonthlyBudgetUsd'),'Unit':'USD'}},NotificationsWithSubscribers=[{
            'Notification':{'ComparisonOperator':'GREATER_THAN','NotificationType':'ACTUAL','Threshold':threshold,'ThresholdType':'PERCENTAGE'},
            'Subscribers':[{'SubscriptionType':'EMAIL','Address':ref('BudgetEmail')}]} for threshold in [50,80,100]]),'Condition':'BudgetEnabled'}
    r['Artifacts']=retained(bucket())
    r['ArtifactsPolicy']=resource('S3::BucketPolicy',Bucket=ref('Artifacts'),PolicyDocument=policy(tls_deny('Artifacts'), {
        'Effect':'Deny','Principal':'*','Action':'s3:PutObject','Resource':sub('${Artifacts.Arn}/releases/*'),
        'Condition':{'Null':{'s3:if-none-match':'true'}}}))
    runtime_statements=[
        allow(['dynamodb:GetItem','dynamodb:Scan','dynamodb:Query','dynamodb:PutItem','dynamodb:UpdateItem','dynamodb:ConditionCheckItem'],sub('arn:${AWS::Partition}:dynamodb:${AWS::Region}:${AWS::AccountId}:table/${ProjectPrefix}-*')),
        allow('sns:Publish',sub('arn:${AWS::Partition}:sns:${AWS::Region}:${AWS::AccountId}:${ProjectPrefix}-*')),
        allow('sqs:SendMessage',sub('arn:${AWS::Partition}:sqs:${AWS::Region}:${AWS::AccountId}:${ProjectPrefix}-*')),
        allow('states:StartSyncExecution',sub('arn:${AWS::Partition}:states:${AWS::Region}:${AWS::AccountId}:stateMachine:${ProjectPrefix}-*')),
        allow(['logs:CreateLogStream','logs:PutLogEvents'],[sub('arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/${ProjectPrefix}-*:*')]),
        allow(LOG_DELIVERY,'*')]
    r['RuntimeBoundary']=resource('IAM::ManagedPolicy',Description='Maximum permissions for auction application roles; no IAM or deployment access.',PolicyDocument=policy(*runtime_statements))
    # CFN needs broad control-plane IDs for API Gateway/CloudFront created during deployment.
    # This is a privileged deployment role, never an application runtime identity.
    tablearn=sub('arn:${AWS::Partition}:dynamodb:${AWS::Region}:${AWS::AccountId}:table/${ProjectPrefix}-*')
    rolearn=sub('arn:${AWS::Partition}:iam::${AWS::AccountId}:role/${ProjectPrefix}/*')
    exec_statements=[
        allow('cognito-idp:CreateUserPool','*',Condition={'StringEquals':{'aws:RequestedRegion':ref('AWS::Region'),'aws:RequestTag/Project':ref('ProjectPrefix')}}),
        allow('cognito-idp:DescribeUserPoolDomain','*',Condition={'StringEquals':{'aws:RequestedRegion':ref('AWS::Region')}}),
        allow(['cognito-idp:DeleteUserPool','cognito-idp:UpdateUserPool','cognito-idp:DescribeUserPool','cognito-idp:SetUserPoolMfaConfig','cognito-idp:GetUserPoolMfaConfig','cognito-idp:TagResource','cognito-idp:UntagResource','cognito-idp:ListTagsForResource','cognito-idp:CreateUserPoolClient','cognito-idp:UpdateUserPoolClient','cognito-idp:DescribeUserPoolClient','cognito-idp:DeleteUserPoolClient','cognito-idp:CreateUserPoolDomain','cognito-idp:UpdateUserPoolDomain','cognito-idp:DeleteUserPoolDomain'],sub('arn:${AWS::Partition}:cognito-idp:${AWS::Region}:${AWS::AccountId}:userpool/*')),
        allow('dynamodb:*',tablearn),
        allow('lambda:*',sub('arn:${AWS::Partition}:lambda:${AWS::Region}:${AWS::AccountId}:function:${ProjectPrefix}-*')),
        allow('states:*',sub('arn:${AWS::Partition}:states:${AWS::Region}:${AWS::AccountId}:stateMachine:${ProjectPrefix}-*')),
        allow('sns:*',sub('arn:${AWS::Partition}:sns:${AWS::Region}:${AWS::AccountId}:${ProjectPrefix}-*')),
        allow('sqs:*',sub('arn:${AWS::Partition}:sqs:${AWS::Region}:${AWS::AccountId}:${ProjectPrefix}-*')),
        allow('events:*',sub('arn:${AWS::Partition}:events:${AWS::Region}:${AWS::AccountId}:rule/${ProjectPrefix}-*')),
        allow('s3:*',[sub('arn:${AWS::Partition}:s3:::${ProjectPrefix}-web-${AWS::AccountId}-${AWS::Region}'),sub('arn:${AWS::Partition}:s3:::${ProjectPrefix}-web-${AWS::AccountId}-${AWS::Region}/*')]),
        allow(['s3:GetObject','s3:GetObjectVersion'],sub('${Artifacts.Arn}/*')),
        allow(['s3:GetBucketLocation','s3:ListBucket'],att('Artifacts')),
        allow(['iam:CreateRole','iam:PutRolePermissionsBoundary'],rolearn,Condition={'ArnEquals':{'iam:PermissionsBoundary':ref('RuntimeBoundary')}}),
        # CFN probes and rolls back nonexistent roles by name before their Path exists.
        allow(['iam:GetRole','iam:GetRolePolicy','iam:DeleteRolePolicy','iam:DeleteRole'],sub('arn:${AWS::Partition}:iam::${AWS::AccountId}:role/${ProjectPrefix}-*')),
        allow(['iam:GetRole','iam:DeleteRole','iam:UpdateAssumeRolePolicy','iam:PutRolePolicy','iam:GetRolePolicy','iam:DeleteRolePolicy','iam:ListRolePolicies','iam:ListAttachedRolePolicies','iam:TagRole','iam:UntagRole','iam:UpdateRole'],rolearn),
        allow('iam:PassRole',rolearn,Condition={'StringEquals':{'iam:PassedToService':['lambda.amazonaws.com','states.amazonaws.com','apigateway.amazonaws.com']}}),
        allow(['apigateway:GET','apigateway:POST','apigateway:PUT','apigateway:PATCH','apigateway:DELETE'],[sub('arn:${AWS::Partition}:apigateway:${AWS::Region}::/restapis*'),sub('arn:${AWS::Partition}:apigateway:${AWS::Region}::/tags/*')]),
        allow('apigateway:UpdateRestApiPolicy',sub('arn:${AWS::Partition}:apigateway:${AWS::Region}::/restapis/*')),
        allow(['cloudfront:CreateDistribution','cloudfront:GetDistribution','cloudfront:GetDistributionConfig','cloudfront:UpdateDistribution','cloudfront:DeleteDistribution','cloudfront:TagResource','cloudfront:UntagResource','cloudfront:ListTagsForResource','cloudfront:CreateOriginAccessControl','cloudfront:GetOriginAccessControl','cloudfront:UpdateOriginAccessControl','cloudfront:DeleteOriginAccessControl','cloudfront:CreateFunction','cloudfront:DescribeFunction','cloudfront:GetFunction','cloudfront:UpdateFunction','cloudfront:PublishFunction','cloudfront:DeleteFunction','cloudfront:CreateCachePolicy','cloudfront:GetCachePolicy','cloudfront:UpdateCachePolicy','cloudfront:DeleteCachePolicy','cloudfront:CreateResponseHeadersPolicy','cloudfront:GetResponseHeadersPolicy','cloudfront:UpdateResponseHeadersPolicy','cloudfront:DeleteResponseHeadersPolicy'],'*'),
        allow(['logs:CreateLogGroup','logs:DeleteLogGroup','logs:PutRetentionPolicy','logs:DeleteRetentionPolicy','logs:TagResource','logs:UntagResource','logs:ListTagsForResource','logs:TagLogGroup','logs:UntagLogGroup','logs:ListTagsLogGroup','logs:PutMetricFilter','logs:DeleteMetricFilter','logs:DescribeMetricFilters'],[
            sub('arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/${ProjectPrefix}-*'),
            sub('arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/vendedlogs/states/${ProjectPrefix}-*'),
            sub('arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/auction/${ProjectPrefix}/*')]),
        allow(LOG_DELIVERY,'*'),
        allow(['cloudwatch:PutMetricAlarm','cloudwatch:DeleteAlarms','cloudwatch:DescribeAlarms','cloudwatch:TagResource','cloudwatch:UntagResource','cloudwatch:ListTagsForResource'],sub('arn:${AWS::Partition}:cloudwatch:${AWS::Region}:${AWS::AccountId}:alarm:${ProjectPrefix}-*'))]
    r['CloudFormationRole']=resource('IAM::Role',RoleName=sub('${ProjectPrefix}-cloudformation'),AssumeRolePolicyDocument=trust('cloudformation.amazonaws.com'),
        Policies=[{'PolicyName':'ProvisionApplication','PolicyDocument':policy(*exec_statements)}])
    r['ApiLoggingRole']={**resource('IAM::Role',AssumeRolePolicyDocument=trust('apigateway.amazonaws.com'),
        ManagedPolicyArns=[sub('arn:${AWS::Partition}:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs')]),'Condition':'ManageLogging'}
    r['ApiLoggingAccount']={**resource('ApiGateway::Account',CloudWatchRoleArn=att('ApiLoggingRole')),'Condition':'ManageLogging'}
    r['GitHubOidc']={**resource('IAM::OIDCProvider',Url='https://token.actions.githubusercontent.com',ClientIdList=['sts.amazonaws.com']),'Condition':'CreateOidc'}
    oidc={'Fn::If':['CreateOidc',ref('GitHubOidc'),ref('ExistingOidcProviderArn')]}
    r['GitHubDeployRole']={**resource('IAM::Role',RoleName=sub('${ProjectPrefix}-github-deploy'),MaxSessionDuration=3600,
        AssumeRolePolicyDocument=policy({'Effect':'Allow','Principal':{'Federated':oidc},'Action':'sts:AssumeRoleWithWebIdentity',
            'Condition':{'StringEquals':{'token.actions.githubusercontent.com:aud':'sts.amazonaws.com',
                 'token.actions.githubusercontent.com:sub':sub('repo:${GitHubRepository}:environment:${GitHubEnvironment}')}}}),
        Policies=[{'PolicyName':'ReviewAndDeploy','PolicyDocument':policy(
            allow(['cloudformation:CreateChangeSet','cloudformation:DescribeChangeSet','cloudformation:ExecuteChangeSet','cloudformation:DeleteChangeSet','cloudformation:DescribeStacks','cloudformation:DescribeStackEvents','cloudformation:GetTemplate'],[
                sub('arn:${AWS::Partition}:cloudformation:${AWS::Region}:${AWS::AccountId}:stack/${ProjectPrefix}-app/*'),sub('arn:${AWS::Partition}:cloudformation:${AWS::Region}:${AWS::AccountId}:changeSet/${ProjectPrefix}-review-*/*')]),
            allow(['cloudformation:DescribeStacks','cloudformation:DescribeStackResources'],sub('arn:${AWS::Partition}:cloudformation:${AWS::Region}:${AWS::AccountId}:stack/${ProjectPrefix}-bootstrap/*')),
            allow('iam:PassRole',att('CloudFormationRole'),Condition={'StringEquals':{'iam:PassedToService':'cloudformation.amazonaws.com'}}),
            allow(['states:ValidateStateMachineDefinition','cloudformation:ValidateTemplate'],'*'),
            allow('apigateway:GET',sub('arn:${AWS::Partition}:apigateway:${AWS::Region}::/account')),
            allow(['s3:GetBucketLocation','s3:ListBucket'],[att('Artifacts'),sub('arn:${AWS::Partition}:s3:::${ProjectPrefix}-web-${AWS::AccountId}-${AWS::Region}')]),
            allow(['s3:GetObject','s3:GetObjectVersion','s3:PutObject'],[sub('${Artifacts.Arn}/releases/*'),sub('arn:${AWS::Partition}:s3:::${ProjectPrefix}-web-${AWS::AccountId}-${AWS::Region}/*')]),
            allow('cloudfront:CreateInvalidation',sub('arn:${AWS::Partition}:cloudfront::${AWS::AccountId}:distribution/*')))}]),'Condition':'UseGitHub'}
    for name,value in {'ArtifactBucket':ref('Artifacts'),'ExecutionRoleArn':att('CloudFormationRole'),'RuntimeBoundaryArn':ref('RuntimeBoundary')}.items(): output(t,name,value)
    t['Outputs']['GitHubRoleArn']={'Value':att('GitHubDeployRole'),'Condition':'UseGitHub'}
    return t


def application():
    t=base('Auction recovery schema v2: authenticated invitation-only demo, API, atomic Express workflows and durable notification outbox.')
    p=t['Parameters']; r=t['Resources']
    p.update({'ArtifactBucket':param(), 'ArtifactPrefix':param(AllowedPattern='releases/[a-f0-9]{64}'),
        'RuntimeBoundaryArn':param(), 'AllowedApiCidr':param(AllowedPattern=r'(\d{1,3}\.){3}\d{1,3}/(32|0)',Description='0.0.0.0/0 for the authenticated public demo; /32 for restricted staging.'),
        'AlertEmail':param(''), 'PointInTimeRecovery':param('true',AllowedValues=['true','false']),
        'ReserveLambdaConcurrency':param('true',AllowedValues=['true','false'],Description='Disable for new accounts whose concurrency quota cannot leave 100 unreserved executions.')})
    t['Conditions']={'EmailAlerts':{'Fn::Not':[{'Fn::Equals':[ref('AlertEmail'),'']}]},'Pitr':{'Fn::Equals':[ref('PointInTimeRecovery'),'true']},'ReserveConcurrency':{'Fn::Equals':[ref('ReserveLambdaConcurrency'),'true']}}
    for name,pk,sk in [('Users','userId',None),('Auctions','auctionId',None),('Bids','auctionId','bidAmt'),('Requests','requestKey',None),('Outbox','eventId',None)]:
        attrs=[{'AttributeName':pk,'AttributeType':'S'}]; keys=[{'AttributeName':pk,'KeyType':'HASH'}]
        if sk: attrs.append({'AttributeName':sk,'AttributeType':'N'}); keys.append({'AttributeName':sk,'KeyType':'RANGE'})
        extra={}
        if name=='Outbox':
            attrs.extend([{'AttributeName':'deliveryStatus','AttributeType':'S'},{'AttributeName':'createdAt','AttributeType':'S'}])
            extra['GlobalSecondaryIndexes']=[{'IndexName':'pending-by-date','KeySchema':[{'AttributeName':'deliveryStatus','KeyType':'HASH'},{'AttributeName':'createdAt','KeyType':'RANGE'}],'Projection':{'ProjectionType':'ALL'}}]
        r[name+'Table']=retained(resource('DynamoDB::Table',TableName=sub('${ProjectPrefix}-'+name.lower()),BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=attrs,KeySchema=keys,PointInTimeRecoverySpecification={'PointInTimeRecoveryEnabled':{'Fn::If':['Pitr',True,False]}},
            SSESpecification={'SSEEnabled':True},Tags=[{'Key':'Project','Value':ref('ProjectPrefix')}],**extra))
        output(t,name+'Table',ref(name+'Table'))
    r['HighBidTopic']=resource('SNS::Topic',TopicName=sub('${ProjectPrefix}-high-bid'))
    r['Alerts']=resource('SNS::Topic',TopicName=sub('${ProjectPrefix}-alerts'))
    r['AlertSubscription']={**resource('SNS::Subscription',TopicArn=ref('Alerts'),Protocol='email',Endpoint=ref('AlertEmail')),'Condition':'EmailAlerts'}
    for name,suffix in [('Notifications','notifications'),('NotificationDlq','notification-dlq'),('ReplayDlq','replay-dlq')]:
        r[name]=retained(resource('SQS::Queue',QueueName=sub('${ProjectPrefix}-'+suffix),SqsManagedSseEnabled=True,MessageRetentionPeriod=1209600,VisibilityTimeout=60))
    r['NotificationQueuePolicy']=resource('SQS::QueuePolicy',Queues=[ref('Notifications'),ref('NotificationDlq')],PolicyDocument=policy(
        allow('sqs:SendMessage',[att('Notifications'),att('NotificationDlq')],Principal={'Service':'sns.amazonaws.com'},Condition={'ArnEquals':{'aws:SourceArn':ref('HighBidTopic')}})))
    r['NotificationSubscription']={**resource('SNS::Subscription',TopicArn=ref('HighBidTopic'),Protocol='sqs',Endpoint=att('Notifications'),RawMessageDelivery=True,
        RedrivePolicy={'deadLetterTargetArn':att('NotificationDlq')}),'DependsOn':'NotificationQueuePolicy'}
    r['WebBucket']=retained(bucket(sub('${ProjectPrefix}-web-${AWS::AccountId}-${AWS::Region}')))
    r['WebOac']=resource('CloudFront::OriginAccessControl',OriginAccessControlConfig={'Name':sub('${ProjectPrefix}-web'),'OriginAccessControlOriginType':'s3','SigningBehavior':'always','SigningProtocol':'sigv4'})
    r['SpaRouter']=resource('CloudFront::Function',Name=sub('${ProjectPrefix}-spa'),AutoPublish=True,
        FunctionConfig={'Comment':'Rewrite only known client routes, never missing assets.','Runtime':'cloudfront-js-2.0'},
        FunctionCode=r"function handler(event) { var r=event.request; if (/^\/(?:users|auctions|help|callback)?\/?$/.test(r.uri) || /^\/bidding\/[^/]+\/?$/.test(r.uri)) r.uri='/index.html'; return r; }")
    r['WebCache']=resource('CloudFront::CachePolicy',CachePolicyConfig={'Name':sub('${ProjectPrefix}-web'),'MinTTL':0,'DefaultTTL':300,'MaxTTL':31536000,
        'ParametersInCacheKeyAndForwardedToOrigin':{'EnableAcceptEncodingGzip':True,'EnableAcceptEncodingBrotli':True,'CookiesConfig':{'CookieBehavior':'none'},'HeadersConfig':{'HeaderBehavior':'none'},'QueryStringsConfig':{'QueryStringBehavior':'none'}}})
    r['WebHeaders']=resource('CloudFront::ResponseHeadersPolicy',ResponseHeadersPolicyConfig={'Name':sub('${ProjectPrefix}-security'),
        'SecurityHeadersConfig':{'ContentTypeOptions':{'Override':True},'FrameOptions':{'FrameOption':'DENY','Override':True},'StrictTransportSecurity':{'AccessControlMaxAgeSec':31536000,'IncludeSubdomains':True,'Override':True},'ReferrerPolicy':{'ReferrerPolicy':'strict-origin-when-cross-origin','Override':True}}})
    r['WebDistribution']=resource('CloudFront::Distribution',DistributionConfig={'Enabled':True,'DefaultRootObject':'index.html','HttpVersion':'http2','IPV6Enabled':True,'PriceClass':'PriceClass_100',
        'Origins':[{'Id':'private-s3','DomainName':att('WebBucket','RegionalDomainName'),'S3OriginConfig':{'OriginAccessIdentity':''},'OriginAccessControlId':ref('WebOac')}],
        'DefaultCacheBehavior':{'TargetOriginId':'private-s3','ViewerProtocolPolicy':'redirect-to-https','AllowedMethods':['GET','HEAD','OPTIONS'],'CachedMethods':['GET','HEAD'],'Compress':True,'CachePolicyId':ref('WebCache'),'ResponseHeadersPolicyId':ref('WebHeaders'),
            'FunctionAssociations':[{'EventType':'viewer-request','FunctionARN':att('SpaRouter','FunctionARN')}]},'ViewerCertificate':{'CloudFrontDefaultCertificate':True}})
    r['WebPolicy']=resource('S3::BucketPolicy',Bucket=ref('WebBucket'),PolicyDocument=policy(tls_deny('WebBucket'),allow('s3:GetObject',sub('${WebBucket.Arn}/*'),Principal={'Service':'cloudfront.amazonaws.com'},Condition={'StringEquals':{'AWS:SourceArn':sub('arn:${AWS::Partition}:cloudfront::${AWS::AccountId}:distribution/${WebDistribution}')}})))
    origin=sub('https://${WebDistribution.DomainName}')
    r['UserPool']=retained(resource('Cognito::UserPool',UserPoolName=sub('${ProjectPrefix}-demo'),UserPoolTier='LITE',
        AdminCreateUserConfig={'AllowAdminCreateUserOnly':True},UsernameAttributes=['email'],AutoVerifiedAttributes=['email'],
        UsernameConfiguration={'CaseSensitive':False},Policies={'PasswordPolicy':{'MinimumLength':12,'RequireLowercase':True,'RequireUppercase':True,'RequireNumbers':True,'RequireSymbols':True,'TemporaryPasswordValidityDays':7}},
        AccountRecoverySetting={'RecoveryMechanisms':[{'Name':'verified_email','Priority':1}]},
        UserPoolTags={'Project':ref('ProjectPrefix')},Schema=[{'Name':'email','AttributeDataType':'String','Required':True,'Mutable':True}]))
    r['UserPoolClient']=resource('Cognito::UserPoolClient',UserPoolId=ref('UserPool'),ClientName=sub('${ProjectPrefix}-web'),GenerateSecret=False,
        AllowedOAuthFlowsUserPoolClient=True,AllowedOAuthFlows=['code'],AllowedOAuthScopes=['openid','email','profile'],SupportedIdentityProviders=['COGNITO'],
        CallbackURLs=[sub('https://${WebDistribution.DomainName}/callback')],LogoutURLs=[sub('https://${WebDistribution.DomainName}/')],
        ExplicitAuthFlows=['ALLOW_REFRESH_TOKEN_AUTH','ALLOW_ADMIN_USER_PASSWORD_AUTH'],PreventUserExistenceErrors='ENABLED',EnableTokenRevocation=True,
        AccessTokenValidity=15,IdTokenValidity=15,RefreshTokenValidity=1,TokenValidityUnits={'AccessToken':'minutes','IdToken':'minutes','RefreshToken':'days'})
    r['UserPoolDomain']=resource('Cognito::UserPoolDomain',UserPoolId=ref('UserPool'),Domain=sub('${ProjectPrefix}-${AWS::AccountId}'),ManagedLoginVersion=1)
    output(t,'UserPoolId',ref('UserPool'));output(t,'UserPoolClientId',ref('UserPoolClient'))
    output(t,'CognitoAuthority',sub('https://cognito-idp.${AWS::Region}.amazonaws.com/${UserPool}'))
    output(t,'CognitoDomain',sub('https://${UserPoolDomain}.auth.${AWS::Region}.amazoncognito.com'))
    functions={
        'UserGetAll':('user_get_all',{'USERS_TABLE':ref('UsersTable')},[allow(['dynamodb:Scan','dynamodb:GetItem'],att('UsersTable'))]),
        'UserCreate':('user_post_create',{'USERS_TABLE':ref('UsersTable')},[allow('dynamodb:PutItem',att('UsersTable'))]),
        'AuctionGetAll':('auction_get_all',{'AUCTIONS_TABLE':ref('AuctionsTable')},[allow('dynamodb:Scan',att('AuctionsTable'))]),
        'AuctionGetOne':('auction_get_one',{'AUCTIONS_TABLE':ref('AuctionsTable')},[allow('dynamodb:GetItem',att('AuctionsTable'))]),
        'AuctionCreate':('auction_post_create',{'AUCTIONS_TABLE':ref('AuctionsTable')},[allow('dynamodb:PutItem',att('AuctionsTable'))]),
        'BidGet':('bid_get_for_auction',{'AUCTIONS_TABLE':ref('AuctionsTable'),'BIDS_TABLE':ref('BidsTable')},[allow('dynamodb:GetItem',att('AuctionsTable')),allow('dynamodb:Query',att('BidsTable'))]),
        'Replay':('notification_replay',{'OUTBOX_TABLE':ref('OutboxTable'),'HIGH_BID_TOPIC_ARN':ref('HighBidTopic'),'MAX_PUBLISH_PER_RUN':'100'},[
            allow(['dynamodb:GetItem','dynamodb:UpdateItem'],att('OutboxTable')),allow('dynamodb:Query',sub('${OutboxTable.Arn}/index/pending-by-date')),allow('sns:Publish',ref('HighBidTopic')),allow('sqs:SendMessage',att('ReplayDlq'))])}
    def role(name, service, statements):
        r[name]=resource('IAM::Role',RoleName=sub('${ProjectPrefix}-'+name),Path=sub('/${ProjectPrefix}/'),PermissionsBoundary=ref('RuntimeBoundaryArn'),
            AssumeRolePolicyDocument=trust(service),Policies=[{'PolicyName':'Application','PolicyDocument':policy(*statements)}])
    for name,(folder,env,statements) in functions.items():
        r[name+'Logs']=retained(resource('Logs::LogGroup',LogGroupName=sub('/aws/lambda/${ProjectPrefix}-'+folder),RetentionInDays=14))
        role(name+'Role','lambda.amazonaws.com',statements+[allow(['logs:CreateLogStream','logs:PutLogEvents'],att(name+'Logs'))])
        r[name]=resource('Lambda::Function',FunctionName=sub('${ProjectPrefix}-'+folder),Runtime='python3.13',Architectures=['arm64'],Handler='lambda_function.lambda_handler',
            Role=att(name+'Role'),MemorySize=256,Timeout=60 if name=='Replay' else 10,ReservedConcurrentExecutions={'Fn::If':['ReserveConcurrency',1,ref('AWS::NoValue')]},
            Code={'S3Bucket':ref('ArtifactBucket'),'S3Key':sub('${ArtifactPrefix}/'+folder+'.zip')},Environment={'Variables':{**env,'ALLOWED_ORIGIN':origin,'REQUIRE_AUTH':'true'}})
    for name in ['Bidding','CloseAuction']:
        r[name+'Logs']=retained(resource('Logs::LogGroup',LogGroupName=sub('/aws/vendedlogs/states/${ProjectPrefix}-'+name),RetentionInDays=14))
        rights=[allow('dynamodb:GetItem',[att('UsersTable'),att('AuctionsTable'),att('RequestsTable')]),
                allow('dynamodb:UpdateItem',[att('AuctionsTable'),att('UsersTable')] if name=='CloseAuction' else [att('AuctionsTable'),att('OutboxTable')]),
                allow('dynamodb:PutItem',[att('RequestsTable')] if name=='CloseAuction' else [att('RequestsTable'),att('BidsTable'),att('OutboxTable')]),allow(LOG_DELIVERY,'*')]
        if name=='Bidding': rights.extend([allow('dynamodb:ConditionCheckItem',att('UsersTable')),allow('sns:Publish',ref('HighBidTopic'))])
        role(name+'Role','states.amazonaws.com',rights)
        r[name]=resource('StepFunctions::StateMachine',StateMachineName=sub('${ProjectPrefix}-'+name),StateMachineType='EXPRESS',RoleArn=att(name+'Role'),
            Definition=json.loads((ROOT/f'hw10/step_functions/{name}.json').read_text()),
            DefinitionSubstitutions={**{k:ref(k) for k in ['UsersTable','AuctionsTable','BidsTable','RequestsTable','OutboxTable']},'HighBidTopicArn':ref('HighBidTopic')},
            LoggingConfiguration={'Level':'ALL','IncludeExecutionData':False,'Destinations':[{'CloudWatchLogsLogGroup':{'LogGroupArn':att(name+'Logs')}}]})
        output(t,name+'Arn',ref(name))
    r['ReplaySchedule']=resource('Events::Rule',Name=sub('${ProjectPrefix}-outbox-replay'),ScheduleExpression='rate(5 minutes)',State='ENABLED',Targets=[{'Id':'Replay','Arn':att('Replay')}])
    r['ReplayPermission']=resource('Lambda::Permission',FunctionName=ref('Replay'),Action='lambda:InvokeFunction',Principal='events.amazonaws.com',SourceArn=att('ReplaySchedule'))
    r['ReplayAsync']=resource('Lambda::EventInvokeConfig',FunctionName=ref('Replay'),Qualifier='$LATEST',MaximumRetryAttempts=2,MaximumEventAgeInSeconds=3600,DestinationConfig={'OnFailure':{'Destination':att('ReplayDlq')}})
    role('ApiStatesRole','apigateway.amazonaws.com',[allow('states:StartSyncExecution',[ref('Bidding'),ref('CloseAuction')])])
    paths={}
    cors={'Access-Control-Allow-Origin':origin,'Access-Control-Allow-Headers':'Content-Type,Authorization','Access-Control-Allow-Methods':'GET,POST,PUT,OPTIONS'}
    for path,method,target,mapping in [('/user','get','UserGetAll',None),('/user','post','UserCreate',None),('/auction','get','AuctionGetAll',None),('/auction','post','AuctionCreate',None),('/auction/{auctionId}','get','AuctionGetOne',None),('/bid','get','BidGet',None),('/bid','post','Bidding','bidPOST'),('/auction','put','CloseAuction','auctionPUT')]:
        operation={'security':[{'Cognito':[]}],'responses':{'200':{'description':'Success'}}}
        if target=='AuctionGetOne': operation['parameters']=[{'name':'auctionId','in':'path','required':True,'type':'string'}]
        if not mapping:
            integration={'type':'aws_proxy','httpMethod':'POST','uri':sub('arn:${AWS::Partition}:apigateway:${AWS::Region}:lambda:path/2015-03-31/functions/${'+target+'.Arn}/invocations')}
            r[target+'Permission']=resource('Lambda::Permission',FunctionName=ref(target),Action='lambda:InvokeFunction',Principal='apigateway.amazonaws.com',SourceArn=sub('arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:${RestApi}/*/'+method.upper()+path.replace('{auctionId}','*')))
        else:
            integration={'type':'aws','httpMethod':'POST','uri':sub('arn:${AWS::Partition}:apigateway:${AWS::Region}:states:action/StartSyncExecution'), 'credentials':att('ApiStatesRole'),'passthroughBehavior':'NEVER','timeoutInMillis':29000,
                'requestTemplates':{'application/json':(ROOT/f'hw10/apigw/request_mappings/{mapping}.vtl').read_text()},
                'responses':{'2\\d{2}':{'statusCode':'200','responseTemplates':{'application/json':(ROOT/f'hw10/apigw/response_mappings/{mapping}.vtl').read_text()},'responseParameters':{'method.response.header.Access-Control-Allow-Origin':sub("'https://${WebDistribution.DomainName}'")}},
                    'default':{'statusCode':'503','responseTemplates':{'application/json':'{"accepted":false,"retryable":true,"message":"Outcome may be unknown. Retry with the same requestId."}'},'responseParameters':{'method.response.header.Access-Control-Allow-Origin':sub("'https://${WebDistribution.DomainName}'")}}}}
            operation['responses']={str(code):{'description':'Application response','headers':{'Access-Control-Allow-Origin':{'type':'string'}}} for code in [200,400,403,404,409,503]}
        operation['x-amazon-apigateway-integration']=integration
        paths.setdefault(path,{})[method]=operation
    for methods in paths.values():
        methods['options']={'responses':{'200':{'description':'CORS','headers':{k:{'type':'string'} for k in cors}}},'x-amazon-apigateway-integration':{
            'type':'mock','requestTemplates':{'application/json':'{"statusCode":200}'},'responses':{'default':{'statusCode':'200','responseParameters':{
                'method.response.header.'+k:sub("'https://${WebDistribution.DomainName}'") if k.endswith('Origin') else "'"+v+"'" for k,v in cors.items()}}}}}
    api_body={'swagger':'2.0','info':{'title':'Auction schema v2','version':'2'},'schemes':['https'],'paths':paths,
        'securityDefinitions':{'Cognito':{'type':'apiKey','name':'Authorization','in':'header','x-amazon-apigateway-authtype':'cognito_user_pools',
            'x-amazon-apigateway-authorizer':{'type':'cognito_user_pools','providerARNs':[att('UserPool')],'identityValidationExpression':sub('^${UserPoolClient}$')}}},
        'x-amazon-apigateway-policy':policy(allow('execute-api:Invoke','execute-api:/*',Principal='*'),{
            'Effect':'Deny','Principal':'*','Action':'execute-api:Invoke','Resource':'execute-api:/*','Condition':{'NotIpAddress':{'aws:SourceIp':ref('AllowedApiCidr')}}}),
        'x-amazon-apigateway-gateway-responses':{name:{'responseParameters':{'gatewayresponse.header.Access-Control-Allow-Origin':sub("'https://${WebDistribution.DomainName}'"),'gatewayresponse.header.Access-Control-Allow-Headers':"'Content-Type,Authorization'",'gatewayresponse.header.Access-Control-Allow-Methods':"'GET,POST,PUT,OPTIONS'"}} for name in ['DEFAULT_4XX','DEFAULT_5XX']}}
    r['RestApi']=resource('ApiGateway::RestApi',Name=sub('${ProjectPrefix}-api'),EndpointConfiguration={'Types':['REGIONAL']},Body=api_body,FailOnWarnings=True,Mode='overwrite')
    deployid='ApiDeployment'+hashlib.sha256(json.dumps(api_body,sort_keys=True).encode()).hexdigest()[:12]
    r[deployid]=resource('ApiGateway::Deployment',RestApiId=ref('RestApi'))
    r['ApiLogs']=retained(resource('Logs::LogGroup',LogGroupName=sub('/auction/${ProjectPrefix}/api'),RetentionInDays=14))
    r['ApiStage']=resource('ApiGateway::Stage',RestApiId=ref('RestApi'),DeploymentId=ref(deployid),StageName='dev',
        Variables={'BiddingStateMachineArn':ref('Bidding'),'CloseAuctionStateMachineArn':ref('CloseAuction')},
        MethodSettings=[{'ResourcePath':'/*','HttpMethod':'*','DataTraceEnabled':False,'LoggingLevel':'ERROR','ThrottlingBurstLimit':10,'ThrottlingRateLimit':5}],
        AccessLogSetting={'DestinationArn':att('ApiLogs'),'Format':'{"requestId":"$context.requestId","status":"$context.status","method":"$context.httpMethod","path":"$context.resourcePath","latency":"$context.responseLatency"}'})
    def alarm(name,namespace,metric,threshold,dimensions=None,stat='Sum',period=300):
        r[name]=resource('CloudWatch::Alarm',AlarmName=sub('${ProjectPrefix}-'+name),Namespace=namespace,MetricName=metric,Statistic=stat,Period=period,EvaluationPeriods=2,Threshold=threshold,
            ComparisonOperator='GreaterThanThreshold',TreatMissingData='notBreaching',AlarmActions=[ref('Alerts')],OKActions=[ref('Alerts')],**({'Dimensions':dimensions} if dimensions else {}))
    for field,metric in [('failed','OutboxFailures'),('oldestPendingAgeSeconds','OutboxOldestAge')]:
        r[metric+'Filter']=resource('Logs::MetricFilter',LogGroupName=ref('ReplayLogs'),FilterPattern='{ $.event = "outbox_summary" }',
            MetricTransformations=[{'MetricNamespace':ref('ProjectPrefix'),'MetricName':metric,'MetricValue':'$.'+field,'DefaultValue':0}])
        alarm(metric,ref('ProjectPrefix'),metric,0 if field=='failed' else 900,stat='Maximum')
    alarm('ReplayErrors','AWS/Lambda','Errors',0,[{'Name':'FunctionName','Value':ref('Replay')}])
    alarm('ReplayThrottles','AWS/Lambda','Throttles',0,[{'Name':'FunctionName','Value':ref('Replay')}])
    alarm('ReplayDeliveryFailed','AWS/Events','FailedInvocations',0,[{'Name':'RuleName','Value':ref('ReplaySchedule')}])
    for name in ['NotificationDlq','ReplayDlq']:
        alarm(name+'Alarm','AWS/SQS','ApproximateNumberOfMessagesVisible',0,[{'Name':'QueueName','Value':att(name,'QueueName')}],stat='Maximum')
    for name in ['Bidding','CloseAuction']:
        for metric in ['ExecutionsFailed','ExecutionsTimedOut']:
            alarm(name+metric,'AWS/States',metric,0,[{'Name':'StateMachineArn','Value':ref(name)}])
    alarm('ApiServerErrors','AWS/ApiGateway','5XXError',0,[{'Name':'ApiName','Value':sub('${ProjectPrefix}-api')},{'Name':'Stage','Value':'dev'}])
    for name,value in {'ApiBaseUrl':sub('https://${RestApi}.execute-api.${AWS::Region}.${AWS::URLSuffix}/dev'),'WebUrl':origin,'WebBucket':ref('WebBucket'),'DistributionId':ref('WebDistribution'),'NotificationQueueUrl':ref('Notifications'),'ReplayFunction':ref('Replay'),'AlertsTopicArn':ref('Alerts')}.items(): output(t,name,value)
    return t


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--check',action='store_true'); args=parser.parse_args()
    for name,content in [('bootstrap',bootstrap()),('application',application())]:
        path=ROOT/f'infra/{name}.json'; expected=json.dumps(content,indent=2,ensure_ascii=False)+'\n'
        if args.check:
            if not path.exists() or path.read_text()!=expected: raise SystemExit(f'Stale generated template: {path}; run python scripts/build_infra.py')
        else: path.parent.mkdir(exist_ok=True); path.write_text(expected)
    print('CloudFormation templates match sources.' if args.check else 'Generated both CloudFormation templates (no AWS calls).')
if __name__=='__main__': main()
