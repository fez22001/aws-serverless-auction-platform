# Invitation-only authentication

The browser uses `oidc-client-ts` with Cognito hosted sign-in and authorization code + PKCE. The client has no secret. OAuth state and tokens use sessionStorage; the app does not store passwords. ID/access tokens expire after 15 minutes and refresh tokens after one day. Sign-out revokes the refresh token and clears the Cognito browser session. API Gateway validates the issuer, signature, expiry and the app-client audience. Already-issued ID tokens can remain usable until expiry after sign-out or account disable; this demo does not implement immediate per-request revocation checks.

All eight business routes require authentication. OPTIONS is anonymous for CORS. The deployed Lambda environment requires trusted authorizer claims. POST /user ignores caller-supplied identity and balance, creates the signed-in subject with 1,000 credits once, and rejects duplicate creation. GET /user returns only that profile. API Gateway overwrites bid identity and supplies the trusted principal to Step Functions. CloseAuction checks the stored immutable owner before changing the auction. Idempotency keys are namespaced by principal.

The invitation is separate from an AWS IAM operator account. Self-registration is disabled. An operator can invite an explicitly chosen recipient:

```bash
.venv/bin/python scripts/invite_user.py --email person@example.com
```

Cognito sends a temporary password. The recipient opens the demo, selects Sign in, enters the invited email and temporary password, and sets their own password. Temporary passwords expire after seven days. Use Cognito's operator console to resend an expired invitation. Do not commit passwords or share AWS administrator credentials with demo visitors.

The client also enables IAM-authorized ADMIN_USER_PASSWORD_AUTH solely for operator-run integration checks. It cannot be invoked with only the public client ID; the GitHub deployment role has no Cognito user-administration permissions.

Legacy development records do not have owners or Cognito identities and are never adopted by newly invited users. Create a new profile and new auctions for the authenticated demo.

## Cloud acceptance check

After deployment, an operator can run `.venv/bin/python scripts/auth_smoke.py`. It creates two disposable identities with email delivery suppressed, checks anonymous access, forged identity/balance fields, owner-only close, idempotency and wrong-client tokens, then removes its test identities and keyed test records. It never prints or persists passwords or tokens. The operator identity, not GitHub Actions, runs these identity-administration checks.
