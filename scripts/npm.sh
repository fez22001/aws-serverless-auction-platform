#!/bin/bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
node_bin="${NODE_BIN:-$(command -v node || true)}"
if [[ -z "$node_bin" ]]; then
  node_bin="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
fi
if [[ ! -x "$node_bin" ]]; then
  echo 'Node is missing. Install the version in .nvmrc or set NODE_BIN to a compatible Node executable.' >&2
  exit 1
fi
export PATH="$(dirname "$node_bin"):$PATH"
export npm_config_cache="$project_root/.tools/npm-cache"
cd "$project_root/ReactAuction"
exec "$node_bin" "$project_root/.tools/npm/package/bin/npm-cli.js" "$@"
