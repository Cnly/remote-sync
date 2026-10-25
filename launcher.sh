#! /usr/bin/env bash

set -euo pipefail

echo "Using config file: $REMOTE_SYNC_CONFIG_FILE" >&2
echo "Using secrets file: $SECRETS_FILE" >&2

for argument in "$@"; do
  case "$argument" in
    -h|--help)
      exec "$REMOTE_SYNC" "$REMOTE_SYNC_PROVIDER" --config-file "$REMOTE_SYNC_CONFIG_FILE" "$@"
      ;;
  esac
done

# Quote arguments for Bash, then its invocation for SOPS's /bin/sh.
printf -v REMOTE_SYNC_COMMAND '%q ' "$REMOTE_SYNC" "$REMOTE_SYNC_PROVIDER" --config-file "$REMOTE_SYNC_CONFIG_FILE" "$@"
printf -v REMOTE_SYNC_COMMAND '%q ' exec bash -c "exec $REMOTE_SYNC_COMMAND"
exec sops exec-env --same-process "$SECRETS_FILE" "$REMOTE_SYNC_COMMAND"
