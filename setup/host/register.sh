#!/bin/sh
# setup/host/register.sh RUNNER...: register runners of this host at the org level, in the
# Default group, with their labels and work directories (design §5.2), then start them.
# Run by the lead, as the runner account, with a fresh registration token on stdin.
# The token is never stored and never on a command line: config.sh reads it from
# ACTIONS_RUNNER_INPUT_TOKEN (and clears it), in an environment that holds only that,
# PATH, HOME, LANG and the runner's three proxy variables where the host has the egress
# tunnel. No runner of the account may be running meanwhile, since job code runs as the
# same account. Auto-update stays on.
set -eu
[ $# -ge 1 ] || { echo "usage: $0 RUNNER... < token" >&2; exit 2; }
etc=${CI_RUNNERS_ETC:-$HOME/data/ci-runners/etc}  # <data>/ci-runners/etc (design §3.2)
case $etc in /*) ;; *) echo "$0: CI_RUNNERS_ETC must be an absolute path" >&2; exit 2 ;; esac
export CI_RUNNERS_ETC=$etc
base=$(dirname "$etc"); data=$(dirname "$base")
ci_run=$base/releases/hook/bin/ci-run
known=$("$ci_run" runners)
running=$(systemctl --user list-units --state=active --plain --no-legend 'ci-runner@*.service' | awk '{print $1}')
[ -z "$running" ] || { echo "register.sh: stop the account's runners first: $running" >&2; exit 1; }
for r in "$@"; do
  echo "$known" | awk -v n="$r" '$1 == n {f=1} END {exit !f}' || { echo "register.sh: $r is not a runner of this host" >&2; exit 2; }
  dir=$data/actions-runner/$r
  [ -x "$dir/config.sh" ] && [ -x "$dir/bin/Runner.Listener" ] || { echo "register.sh: $dir has no runner app: apply the setup plan first" >&2; exit 1; }
  [ ! -e "$dir/.runner" ] || { echo "register.sh: $r is registered already" >&2; exit 1; }
done
IFS= read -r token || true
[ -n "$token" ] || { echo "register.sh: no token on stdin" >&2; exit 2; }
for r in "$@"; do
  dir=$data/actions-runner/$r
  labels=$(echo "$known" | awk -v n="$r" '$1 == n {print $2}')
  # the proxy settings, read as data: only these three names, nothing executed
  px=$(grep -E '^(https_proxy|http_proxy|no_proxy)=[A-Za-z0-9.:/,_-]*$' "$dir/.env" || true)
  [ "$(echo "$px" | grep -c .)" = "$(grep -cE '^(https_proxy|http_proxy|no_proxy)=' "$dir/.env" || true)" ] ||
    { echo "register.sh: $dir/.env has a proxy line that is not one word of [A-Za-z0-9.:/,_-]" >&2; exit 1; }
  # printf is a shell builtin: the token reaches the child's stdin, not an argv
  printf '%s\n' "$token" | (cd "$dir" && env -i PATH=/usr/bin:/bin HOME="$HOME" LANG=C.UTF-8 $px \
    /bin/sh -c 'IFS= read -r ACTIONS_RUNNER_INPUT_TOKEN && export ACTIONS_RUNNER_INPUT_TOKEN && exec ./config.sh "$@"' \
    config.sh --unattended --url https://github.com/chaobrain --name "$r" --runnergroup Default \
    --labels "$labels" --work "$data/ci-work/$r")
done
token=
for r in "$@"; do
  systemctl --user enable --now "ci-runner@$r.service"
done
