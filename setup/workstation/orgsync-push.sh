#!/bin/bash
# setup/workstation/orgsync-push.sh ORG SSH_HOST REMOTE_DATA REMOTE_PYTHON [SSH_HOST REMOTE_DATA REMOTE_PYTHON]...:
# push the org snapshot to each host that holds no token (design decisions 11.9, 12.4,
# §6.5). Run by the maintainer's ci-orgsync-push.timer on the workstation, as the
# maintainer. It fetches the snapshot once with the maintainer's own gh login (read here,
# for this run only, and passed to ci-run in its environment), then pipes it over ssh to
# each host in turn: <REMOTE_DATA>/ci-runners/releases/hook/bin/ci-run org-sync --receive,
# which checks it and writes etc/org.json. On a 3090 host the CI account's key line forces
# that command itself. Each host is bounded on its own (PUSH_TIMEOUT s, default 120), and a
# host that fails does not hold up the others: it ages on its own (stale after 60 min).
# The status is 1 if any host failed. A failed fetch sends nothing: the old snapshots stay.
set -euo pipefail
[ $# -ge 4 ] && [ $(( ($# - 1) % 3 )) -eq 0 ] ||
  { echo "usage: $0 ORG SSH_HOST REMOTE_DATA REMOTE_PYTHON [SSH_HOST REMOTE_DATA REMOTE_PYTHON]..." >&2; exit 2; }
for a in "$@"; do  # each goes into the remote command line: one plain word each
  [[ $a =~ ^[A-Za-z0-9._/][A-Za-z0-9._/-]*$ ]] ||  # no leading -: ssh would read an option
    { echo "orgsync-push.sh: an argument is not one word of [A-Za-z0-9._/-], or starts with -" >&2; exit 2; }
done
org=$1
shift
args=("$@")
for ((i = 0; i < ${#args[@]}; i += 3)); do
  for a in "${args[i + 1]}" "${args[i + 2]}"; do
    case $a in /*) ;; *) echo "orgsync-push.sh: REMOTE_DATA and REMOTE_PYTHON must be absolute" >&2; exit 2 ;; esac
  done
done
bound=${PUSH_TIMEOUT:-120}
[[ $bound =~ ^[1-9][0-9]{0,3}$ ]] || { echo "orgsync-push.sh: PUSH_TIMEOUT=$bound is not a number of seconds" >&2; exit 2; }
root=$(dirname "$(dirname "$(dirname "$(readlink -f "$0")")")")  # the release: setup/workstation/ -> root
token=$(gh auth token)
doc=$(GH_TOKEN=$token "$root/bin/ci-run" org-sync --fetch --org "$org")
unset token
failed=0
for ((i = 0; i < ${#args[@]}; i += 3)); do
  host=${args[i]} data=${args[i + 1]} py=${args[i + 2]}
  if printf '%s\n' "$doc" | timeout -k 10 "$bound" ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 \
      "$host" "CI_RUNNERS_ETC=$data/ci-runners/etc CI_RUNNERS_PYTHON=$py exec $data/ci-runners/releases/hook/bin/ci-run org-sync --receive"; then
    echo "orgsync-push.sh: $host: pushed"
  else
    echo "orgsync-push.sh: $host: failed (status $?)" >&2
    failed=1
  fi
done
exit $failed
