# Deployment and CI/CD

## Bootstrap once as an operator

Copy `infra/deploy.example.json` to ignored `infra/deploy.local.json`. Set the expected account, named short-lived AWS profile, prefix, region, alert email and GitHub repository. The templates target us-east-1. Use `allowed_api_cidr: "0.0.0.0/0"` for the authenticated demo, or an actual public IPv4 /32 for additional network restriction.

```bash
python3 -m venv .tools/infra-venv
.tools/infra-venv/bin/pip install -r local/requirements-infra.txt
.tools/infra-venv/bin/cfn-lint infra/bootstrap.json infra/application.json
release_dir="$(python3 scripts/release.py build)"
.venv/bin/python scripts/release.py plan-bootstrap --release "$release_dir" --out .local/bootstrap-plan.json
# Review IAM, resource changes and the exact digest before applying.
.venv/bin/python scripts/release.py apply --plan .local/bootstrap-plan.json --reviewed-digest REVIEWED_SHA256
```

Bootstrap is operator-managed and cannot be updated by the GitHub deployment role. It owns the private artifact bucket, CloudFormation execution role, runtime permissions boundary, GitHub OIDC trust, optional regional API logging role, and optional account-wide monthly cost budget. Reuse an existing OIDC provider using `existing_oidc_provider_arn` if needed. Avoid overwriting another application's regional API Gateway logging configuration.

## Configure GitHub

The `dev` environment must allow deployments only from `main`, require an owner review, and disallow administrator bypass. Its variables are `AWS_ROLE_ARN` (bootstrap output), `AWS_ACCOUNT_ID`, `PROJECT_PREFIX`, and `ALLOWED_API_CIDR`. Put `ALERT_EMAIL` in an **environment secret**, not in the source. Fork pull requests run offline CI with read-only permissions and receive no AWS identity.

Set `github_subject_prefix` to the exact `sub_claim_prefix` returned by `gh api repos/OWNER/REPO/actions/oidc/customization/sub`. New GitHub.com repositories use immutable owner/repository IDs, for example `repo:OWNER@123/REPO@456`. The AWS trust policy accepts only this exact prefix followed by `:environment:dev`, with audience `sts.amazonaws.com`. Do not remove the immutable IDs or use wildcards. Action versions are pinned to commit SHAs. The default GitHub token is read-only. No AWS access keys are stored in GitHub.

## Release

1. Push changes or open a PR; `Auction offline validation` runs tests, frontend lint/build, template checks and creates a reproducible release artifact.
2. Manually run `Auction reviewed deployment` on main with `operation=plan`. Approve the dev environment. Review the resource-change table and SHA-256 digest in the run summary. Raw plans stay in private S3 because they contain the alert email; the public artifact holds only an immutable reference and digest.
3. Run the same workflow with `operation=apply`, the successful plan run ID and its reviewed digest. The workflow requires the same commit and verifies the live AWS change set before executing it. It waits for successful stack completion.
4. Run `operation=publish-web`. It builds with the API and Cognito values from stack outputs, uploads assets before index.html, records the prior S3 version and invalidates CloudFront. Check the site and the invalidation status.

Every cloud run needs environment approval. Main requires the successful CI check; force pushes and branch deletion are disabled. A sole maintainer may submit and approve their own environment run; the explicit approval is still required.

For local operator deployment use `plan-app`, `apply`, `wait-app`, and `publish-web` with `scripts/release.py`. A web rollback can restore the recorded `previous_index_version` using `rollback-web --index-version VERSION --out .local/rollback.json`. Review API compatibility before rolling frontend versions backward.

## Cost and retention

The budget is an alert, not a spend cap. A paid AWS Support plan is billed separately from project design assumptions. API throttling is 5 requests/s with a burst of 10. Logs retain 14 days. DynamoDB, queues, S3 and Cognito user pools have retain policies; deleting the application stack intentionally does not erase their data. Review and explicitly clean retained resources when retiring the demo. Confirm the SNS alert email subscription after initial creation.
