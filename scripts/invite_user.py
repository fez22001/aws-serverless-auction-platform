"""Operator-only: send a Cognito demo invitation to an explicitly chosen address."""
import argparse
from pathlib import Path
from release import config, connect, describe_stack, outputs, stack_name

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=Path('infra/deploy.local.json'));p.add_argument('--email',required=True)
    a=p.parse_args();c=config(a.config);client,_=connect(c)
    out=outputs(describe_stack(client('cloudformation'),stack_name(c,'app')))
    client('cognito-idp').admin_create_user(UserPoolId=out['UserPoolId'],Username=a.email,
        UserAttributes=[{'Name':'email','Value':a.email}],DesiredDeliveryMediums=['EMAIL'])
    print('Invitation requested. Open the demo and sign in with the temporary password from Cognito.')
    print(out['WebUrl'])
