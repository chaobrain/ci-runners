#!/bin/sh
# setup/apply.sh ROLE --plan FILE: make exactly the approved plan FILE; refuses if a
# fresh plan differs (design §5.2). Writes state/setup.json.
set -eu
[ $# -eq 3 ] && [ "$2" = --plan ] || { echo "usage: $0 ROLE --plan FILE" >&2; exit 2; }
etc=${CI_RUNNERS_ETC:-$HOME/data/ci-runners/etc}  # <data>/ci-runners/etc (design §3.2)
case $etc in /*) ;; *) echo "$0: CI_RUNNERS_ETC must be an absolute path" >&2; exit 2 ;; esac
export CI_RUNNERS_ETC=$etc
base=$(dirname "$etc")
exec "$base/releases/hook/bin/ci-run" apply --plan "$3"
