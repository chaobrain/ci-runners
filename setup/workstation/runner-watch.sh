#!/bin/bash
# setup/workstation/runner-watch.sh [--report-only] STATE NET_WRAP ORG HOST...: an issue when a
# host has no runner online (design decision 15.6). Run by the maintainer's ci-runner-watch.timer
# on 3090b, as the maintainer, every quarter hour. It runs this release's ci-run runner-watch
# under NET_WRAP, the host's proxy wrapper: it reads the org's self-hosted runners, groups them
# by HOST (a runner's name or label is HOST or starts with HOST-), and keeps one issue
# "No runner of <host> online" in <ORG>/ci-runners while a host has had no runner online at two
# consecutive checks; STATE, an absolute path, keeps the counts. The login is the maintainer's
# own, read from gh here, for this run only, and passed to ci-run in its environment (GH_TOKEN),
# never on a command line or in a file. --report-only is passed on: it prints what it would do
# and writes nothing (the dry run). The status is ci-run's.
set -euo pipefail
report=()
if [ "${1:-}" = --report-only ]; then
  report=(--report-only)
  shift
fi
[ $# -ge 4 ] || { echo "usage: $0 [--report-only] STATE NET_WRAP ORG HOST..." >&2; exit 2; }
state=$1 wrap=$2 org=$3
shift 3
for a in "$state" "$wrap"; do  # absolute paths, one plain word each
  [[ $a =~ ^/[A-Za-z0-9._/-]*$ ]] ||
    { echo "runner-watch.sh: STATE or NET_WRAP is not an absolute path of one word of [A-Za-z0-9._/-]" >&2; exit 2; }
done
[[ $org =~ ^[A-Za-z0-9][A-Za-z0-9-]*$ ]] || { echo "runner-watch.sh: ORG is not an org name" >&2; exit 2; }
for h in "$@"; do  # no leading -: ci-run would read an option
  [[ $h =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] ||
    { echo "runner-watch.sh: a HOST is not one word of [A-Za-z0-9._-], or starts with -" >&2; exit 2; }
done
[ -f "$wrap" ] && [ -x "$wrap" ] || { echo "runner-watch.sh: $wrap is not an executable file" >&2; exit 2; }
root=$(dirname "$(dirname "$(dirname "$(readlink -f "$0")")")")  # the release: setup/workstation/ -> root
echo "runner-watch.sh: release $(cat "$root/actions/RELEASE" 2>/dev/null || echo '?') at $root"
# ci-run reads none of these, but none of the user manager's may stand in for the login
unset GITHUB_TOKEN GITHUB_REPOSITORY GITHUB_ACTIONS GITHUB_OUTPUT GITHUB_STEP_SUMMARY
token=$(gh auth token)
[ -n "$token" ] || { echo "runner-watch.sh: gh gave no login" >&2; exit 2; }
GH_TOKEN=$token exec "$wrap" "$root/bin/ci-run" runner-watch --org "$org" --state "$state" "${report[@]}" -- "$@"
