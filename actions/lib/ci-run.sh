#!/bin/bash
# actions/lib/ci-run.sh CMD ARGS...: what every action runs (design §9). On one of our
# runners it executes the host layer of this action's release, named in actions/RELEASE
# (stamped at release; `hook` on main, the hook's release): <data>/ci-runners/releases/
# <release>/bin/ci-run, where <data>/ci-runners/etc is the runner's CI_RUNNERS_ETC (its
# .env sets it; ~/data without it). If the release is not installed here, a part ends
# `unavailable` and goes to its fallback on another runner of its pool; any other
# command fails.
set -uo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cmd=${1:?usage: ci-run.sh CMD ARGS...}
shift
release=$(tr -d '[:space:]' < "$here/../RELEASE")
if ! [[ $release =~ ^(hook|v[0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
  echo "::error::ci-runners: bad RELEASE '$release'"
  exit 2
fi
etc=${CI_RUNNERS_ETC:-}
if [[ $etc == /* ]]; then rel=$(dirname "$etc")/releases; else rel=$HOME/data/ci-runners/releases; fi
ci_run=$rel/$release/bin/ci-run
if [ -x "$ci_run" ]; then
  exec "$ci_run" "$cmd" "$@"
fi
echo "::error::ci-runners: release $release is not installed on this host"
[ "$cmd" = part ] || exit 2
reason="release $release is not installed on this host"
own=${CI_RUNNER_POOL_LABELS:-}  # the pool's own labels (design §5.3)
next=none; retry_on='[]'
if [[ $own =~ ^[a-z0-9][a-z0-9-]*(,[a-z0-9][a-z0-9-]*)*$ ]]; then
  next=fallback; retry_on="[\"self-hosted\",\"${own//,/\",\"}\"]"
fi
{ echo "outcome=unavailable"; echo "reason=$reason"; echo "next=$next"; echo "retry-on=$retry_on"; } >> "${GITHUB_OUTPUT:-/dev/null}"
id=
while [ $# -gt 1 ]; do [ "$1" = --id ] && { id=$2; break; }; shift; done  # the first --id: the action passes it before --command
if [ "$cmd" = part ] && [[ $id =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] && [ -n "${RUNNER_TEMP:-}" ]; then
  art=$RUNNER_TEMP/ci-runners/$id  # what the part action uploads, as part's own outcome would be
  run_attempt=${GITHUB_RUN_ATTEMPT:-0}  # no record here: the verdict orders attempts by it (issue #64)
  [[ $run_attempt =~ ^[1-9][0-9]{0,3}$ ]] || run_attempt=0
  mkdir -p "$art" && printf '{"outcome": "unavailable", "reason": "%s", "next": "%s", "retry-on": %s, "run-attempt": %s}\n' \
    "$reason" "$next" "$retry_on" "$run_attempt" > "$art/outcome.json"
fi
exit 0
