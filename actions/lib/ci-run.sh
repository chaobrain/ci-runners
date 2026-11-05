#!/bin/bash
# actions/lib/ci-run.sh CMD ARGS...: what every action runs (design §9). On one of our
# runners it executes the host layer of this action's release, named in actions/RELEASE
# (stamped at release; `hook` on main, the hook's release): ~/data/ci-runners/releases/
# <release>/bin/ci-run. On a GitHub-hosted runner (public consumers' claim) it runs the
# package of this action's own checkout, with --hosted. If the release is not installed
# here, a part ends `unavailable` and goes to its fallback; any other command fails.
set -uo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cmd=${1:?usage: ci-run.sh CMD ARGS...}
shift
if [ "${RUNNER_ENVIRONMENT:-}" = github-hosted ]; then
  [ "$cmd" = claim ] || { echo "::error::ci-runners: $cmd runs on our runners only"; exit 2; }
  exec "$here/../../bin/ci-run" claim --hosted "$@"
fi
release=$(tr -d '[:space:]' < "$here/../RELEASE")
case $release in hook|v[0-9]*.[0-9]*.[0-9]*) ;; *) echo "::error::ci-runners: bad RELEASE '$release'"; exit 2 ;; esac
ci_run=$HOME/data/ci-runners/releases/$release/bin/ci-run
if [ ! -x "$ci_run" ]; then
  echo "::error::ci-runners: release $release is not installed on this host"
  if [ "$cmd" = part ] || [ "$cmd" = salvage ]; then
    { echo "outcome=unavailable"; echo "reason=release $release is not installed on this host"
      echo "next=fallback"; echo 'retry-on=["self-hosted"]'; } >> "${GITHUB_OUTPUT:-/dev/null}"
    exit 0
  fi
  exit 2
fi
exec "$ci_run" "$cmd" "$@"
