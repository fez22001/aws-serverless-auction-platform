# Security and demo limits

Report security issues privately through the repository owner's GitHub profile; do not post credentials, tokens, personal data or exploit details in public issues.

This is an invitation-only educational demo, not a production marketplace. No payments are collected, fictional credits have no monetary value, and auction items are examples. Invited accounts can create and browse auctions and their pseudonymous bid history. Profiles are private to the signed-in user. Use fictional names and descriptions.

Authentication does not remove all abuse or cost risks. There is global API throttling but no per-account quota, CAPTCHA, WAF, or immediate ID-token revocation list. Account invitations must be controlled by the operator. A user can bid on their own auction; the demo does not implement marketplace fraud rules. Credits are not reserved across auctions, so closing can reject a winner whose remaining balance became insufficient. List endpoints paginate DynamoDB internally but are not designed for large datasets.

The notification outbox delivers at least once; consumers must deduplicate event IDs. Idempotency receipts are retained indefinitely in this small demo. Reconciliation, data export/deletion, production recovery exercises, and strong transaction-level business limits would be needed before broader use.

The public repository excludes deployment configuration, credentials, local reports and old private course history. Generated bundles contain public Cognito/API identifiers only. Never put credentials in VITE_* variables. Runtime IAM roles cannot deploy resources or administer identities; the CloudFormation execution role is privileged and restricted to the deployment path.
