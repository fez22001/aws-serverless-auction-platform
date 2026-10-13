#!/bin/bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"
mkdir -p .tools
# curl uses the macOS trust store; TLS verification remains enabled.
curl --fail --silent --show-error https://registry.npmjs.org/npm/11.6.2 -o .tools/npm-metadata.json
curl --fail --silent --show-error https://registry.npmjs.org/npm/-/npm-11.6.2.tgz -o .tools/npm.tgz
python3 - <<'PY'
import base64, hashlib, json, tarfile
from pathlib import Path
metadata = json.loads(Path('.tools/npm-metadata.json').read_text())
data = Path('.tools/npm.tgz').read_bytes()
assert 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode() == metadata['dist']['integrity']
with tarfile.open('.tools/npm.tgz') as archive:
    archive.extractall('.tools/npm', filter='data')
PY
bash scripts/npm.sh ci --no-audit --no-fund
bash scripts/npm.sh ci --prefix ../local/asl --no-audit --no-fund
python3 -m venv .venv
.venv/bin/python -m pip install --cache-dir .tools/pip-cache -r local/requirements-dev.txt
if [[ ! -f ReactAuction/.env.local ]]; then
  cp ReactAuction/.env.example ReactAuction/.env.local
fi
echo 'Local dependencies ready. Start with: python3 scripts/run_local.py'
