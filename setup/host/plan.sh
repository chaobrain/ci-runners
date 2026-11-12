#!/bin/sh
# setup/host/plan.sh ROLE [--adopt]: print every change setup would make on this host, as the
# runner account; changes nothing (design §5.2). The lead approves its output. --adopt
# takes over ci-* units that no apply recorded (an older release wrote them).
set -eu
[ $# -eq 1 ] || { [ $# -eq 2 ] && [ "$2" = --adopt ]; } || { echo "usage: $0 ROLE [--adopt]" >&2; exit 2; }
etc=${CI_RUNNERS_ETC:-$HOME/data/ci-runners/etc}  # <data>/ci-runners/etc (design §3.2)
case $etc in /*) ;; *) echo "$0: CI_RUNNERS_ETC must be an absolute path" >&2; exit 2 ;; esac
export CI_RUNNERS_ETC=$etc
base=$(dirname "$etc")
ci_run=$base/releases/hook/bin/ci-run
role=$("$ci_run" role)
[ "$role" = "$1" ] || { echo "plan.sh: this host's inventory is for $role, not $1" >&2; exit 2; }
exec "$ci_run" plan ${2:+"$2"}
