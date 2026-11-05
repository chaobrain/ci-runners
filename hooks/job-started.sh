#!/bin/sh
# The runner's job-started hook (ACTIONS_RUNNER_HOOK_JOB_STARTED), installed as
# <data>/ci-runners/hooks/job-started.sh (design §3.2, §6.3). It runs the hook of the
# release that ../releases/hook points at, isolated, and kills it if its own 60 s bound
# fails (a refusal: any non-zero exit refuses the job).
here=$(/usr/bin/dirname "$(/usr/bin/readlink -f "$0")")
root=$(/usr/bin/readlink -f "$here/../releases/hook") || root=
if [ -z "$root" ] || [ ! -d "$root/ci_runners" ]; then
  echo "ci-runners: refused error policy=none"
  exit 1
fi
# the interpreter: setup writes the inventory's python into this line when it installs
# the hook (design §3.2); the environment never chooses it, since a job's could
py=/usr/bin/python3  # @CI_PYTHON@
exec /usr/bin/timeout -k 5 65 "$py" -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from ci_runners.hook import main; sys.exit(main(sys.argv[1:]))' "$root" job-started
