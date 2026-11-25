#!/bin/sh
# Run the integration suite's Radicale without Docker: ./tests/integration/radicale-local.sh
# Listens on 127.0.0.1:${RADICALE_PORT:-5232} with the account from tests/integration/radicale/users;
# state lives in a temporary directory and is removed on exit.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
data=$(mktemp -d)
trap 'rm -rf "$data"' EXIT INT TERM
cat > "$data/config" <<CONF
[server]
hosts = 127.0.0.1:${RADICALE_PORT:-5232}
[auth]
type = htpasswd
htpasswd_filename = $here/radicale/users
htpasswd_encryption = plain
[storage]
filesystem_folder = $data/collections
[rights]
type = owner_only
CONF
uvx radicale --config "$data/config"
