#!/bin/sh
# setup/apply.sh ROLE --plan FILE: make exactly the approved plan FILE; refuses if a
# fresh plan differs (design §5.2). Writes state/setup.json.
set -eu
[ $# -eq 3 ] && [ "$2" = --plan ] || { echo "usage: $0 ROLE --plan FILE" >&2; exit 2; }
exec "$HOME/data/ci-runners/releases/hook/bin/ci-run" apply --plan "$3"
