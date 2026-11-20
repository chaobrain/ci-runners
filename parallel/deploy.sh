#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "${CI_RUNNERS_PYTHON:-python3}" -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from parallel.deploy import main; sys.exit(main(sys.argv[1:]))' "$root" "$@"
