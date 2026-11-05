#!/bin/sh
# setup/plan.sh ROLE: print every change setup would make on this host, as the runner
# account; changes nothing (design §5.2). The lead approves its output.
set -eu
[ $# -eq 1 ] || { echo "usage: $0 ROLE" >&2; exit 2; }
ci_run=$HOME/data/ci-runners/releases/hook/bin/ci-run
role=$("$ci_run" role)
[ "$role" = "$1" ] || { echo "plan.sh: this host's inventory is for $role, not $1" >&2; exit 2; }
exec "$ci_run" plan
