#! /usr/bin/env bash

set -euo pipefail

echo "Using secrets file: $WRANGLER_SECRETS_FILE" >&2

command=("$WRANGLER_BIN")

if [[ -n ${WORKER_CONFIG:-} ]]; then
  project="$PWD/.wrangler/$WORKER_NAME"
  workspace="$project/deploy"
  source=$WORKER_SOURCE

  if [[ ${1:-} == dev ]]; then
    if [[ -z ${WORKER_RELATIVE_SOURCE:-} || ! -d $PWD/$WORKER_RELATIVE_SOURCE ]]; then
      echo "Run nix run .#sync-worker-$WORKER_NAME -- dev from the consuming flake root to use its live source directory." >&2
      exit 1
    fi
    source="$PWD/$WORKER_RELATIVE_SOURCE"
    workspace="$project/dev"
    WORKER_CONFIG=$WORKER_DEV_CONFIG
  fi

  # Separate configs/source links let deployment coexist with a running dev
  # server. Both use the same persistent Wrangler cache and local data.
  mkdir -p "$workspace" "$project/runtime"
  ln -sfnT "$source" "$workspace/source"
  ln -sfnT "$project/runtime" "$workspace/.wrangler"
  install -m 644 "$WORKER_CONFIG" "$workspace/wrangler.json"
  command+=("--config=$workspace/wrangler.json")
fi
command+=("$@")

# SOPS invokes /bin/sh; quote argv for Bash and preserve it through that shell.
printf -v quoted_command '%q ' "${command[@]}"
printf -v quoted_command '%q ' exec bash -c "exec $quoted_command"
exec sops exec-env --same-process "$WRANGLER_SECRETS_FILE" "$quoted_command"
