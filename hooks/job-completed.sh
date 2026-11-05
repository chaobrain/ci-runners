#!/bin/sh
# The runner's job-completed hook (ACTIONS_RUNNER_HOOK_JOB_COMPLETED), installed as
# ~/data/ci-runners/hooks/job-completed.sh (design §3.2, §6.3). It runs the hook of the
# release that ../releases/hook points at, with the system Python, isolated.
here=$(dirname "$(readlink -f "$0")")
root=$(readlink -f "$here/../releases/hook") || root=
if [ -z "$root" ] || [ ! -d "$root/ci_runners" ]; then
  exit 0  # nothing to clean up with
fi
exec /usr/bin/python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from ci_runners.hook import main; sys.exit(main(sys.argv[1:]))' "$root" job-completed
