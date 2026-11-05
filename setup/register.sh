#!/bin/sh
# setup/register.sh RUNNER: register one runner of this host at the org level, in the
# Default group, with its labels and work directory (design §5.2), then start it. Run
# by the lead, as the runner account, with a fresh registration token on stdin (never
# stored). Auto-update stays on.
set -eu
[ $# -eq 1 ] || { echo "usage: $0 RUNNER < token" >&2; exit 2; }
ci_run=$HOME/data/ci-runners/releases/hook/bin/ci-run
line=$("$ci_run" runners | awk -v n="$1" '$1 == n')
[ -n "$line" ] || { echo "register.sh: $1 is not a runner of this host" >&2; exit 2; }
labels=$(echo "$line" | cut -d' ' -f2)
dir=$HOME/data/actions-runner/$1
[ -x "$dir/config.sh" ] || { echo "register.sh: $dir has no runner app: apply the setup plan first" >&2; exit 1; }
[ ! -e "$dir/.runner" ] || { echo "register.sh: $1 is registered already" >&2; exit 1; }
IFS= read -r token
set -a; . "$dir/.env"; set +a  # the CI proxy
cd "$dir"
./config.sh --unattended --url https://github.com/chaobrain --token "$token" --name "$1" \
  --runnergroup Default --labels "$labels" --work "$HOME/data/ci-work/$1"
systemctl --user enable --now "ci-runner@$1.service"
