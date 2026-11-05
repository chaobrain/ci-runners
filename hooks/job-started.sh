#!/bin/sh
# The runner's job-started hook (ACTIONS_RUNNER_HOOK_JOB_STARTED), installed as
# ~/data/ci-runners/hooks/job-started.sh (design §3.2, §6.3). It runs the hook of the
# release that ../releases/hook points at, with the system Python, isolated, and
# kills it if its own 60 s bound fails (a refusal: any non-zero exit refuses the job).
here=$(dirname "$(readlink -f "$0")")
root=$(readlink -f "$here/../releases/hook") || root=
if [ -z "$root" ] || [ ! -d "$root/ci_runners" ]; then
  echo "ci-runners: refused error policy=none"
  exit 1
fi
exec /usr/bin/timeout -k 5 65 /usr/bin/python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from ci_runners.hook import main; sys.exit(main(sys.argv[1:]))' "$root" job-started
