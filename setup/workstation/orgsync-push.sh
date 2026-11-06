#!/bin/bash
# setup/workstation/orgsync-push.sh ORG SSH_HOST REMOTE_DATA REMOTE_PYTHON: push the org
# snapshot to a host that holds no token (design decision 11.9, §6.5). Run by the
# maintainer's ci-orgsync-push.timer on the workstation, as the maintainer. It fetches
# the snapshot with the maintainer's own gh login (read here, for this run only, and
# passed to ci-run in its environment), and pipes it over ssh to
# <REMOTE_DATA>/ci-runners/releases/hook/bin/ci-run org-sync --receive, which checks it
# and writes etc/org.json. A failed fetch sends nothing: the old snapshot stays, and
# the hook refuses once it is stale.
set -euo pipefail
[ $# -eq 4 ] || { echo "usage: $0 ORG SSH_HOST REMOTE_DATA REMOTE_PYTHON" >&2; exit 2; }
for a in "$@"; do  # each goes into the remote command line: one plain word each
  [[ $a =~ ^[A-Za-z0-9._/][A-Za-z0-9._/-]*$ ]] ||  # no leading -: ssh would read an option
    { echo "orgsync-push.sh: an argument is not one word of [A-Za-z0-9._/-], or starts with -" >&2; exit 2; }
done
org=$1 host=$2 data=$3 py=$4
for a in "$data" "$py"; do
  case $a in /*) ;; *) echo "orgsync-push.sh: REMOTE_DATA and REMOTE_PYTHON must be absolute" >&2; exit 2 ;; esac
done
root=$(dirname "$(dirname "$(dirname "$(readlink -f "$0")")")")  # the release: setup/workstation/ -> root
token=$(gh auth token)
doc=$(GH_TOKEN=$token "$root/bin/ci-run" org-sync --fetch --org "$org")
unset token
printf '%s\n' "$doc" | ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 "$host" \
  "CI_RUNNERS_ETC=$data/ci-runners/etc CI_RUNNERS_PYTHON=$py exec $data/ci-runners/releases/hook/bin/ci-run org-sync --receive"
