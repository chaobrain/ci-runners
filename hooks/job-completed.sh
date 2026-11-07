#!/bin/sh
# The runner's job-completed hook (ACTIONS_RUNNER_HOOK_JOB_COMPLETED), installed as
# <data>/ci-runners/hooks/job-completed.sh (design §3.2, §6.3), and copied into the CI
# account's private /tmp on a 3090 host (13.5). It runs the hook of the release that
# <base>/releases/hook points at, isolated.
# the release: setup writes this host's <data>/ci-runners into this line when it installs
# the hook (decision 13.5), so that a copy in /tmp/ci-runners-hooks runs the same release
base=$(/usr/bin/dirname "$(/usr/bin/dirname "$(/usr/bin/readlink -f "$0")")")  # @CI_BASE@
root=$(/usr/bin/readlink -f "$base/releases/hook") || root=
if [ -z "$root" ] || [ ! -d "$root/ci_runners" ]; then
  exit 0  # nothing to clean up with
fi
# the interpreter: setup writes the inventory's python into this line when it installs
# the hook (design §3.2); the environment never chooses it, since a job's could
py=/usr/bin/python3  # @CI_PYTHON@
exec "$py" -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from ci_runners.hook import main; sys.exit(main(sys.argv[1:]))' "$root" job-completed
