#!/bin/sh
# The runner's job-started hook (ACTIONS_RUNNER_HOOK_JOB_STARTED), installed as
# <data>/ci-runners/hooks/job-started.sh (design §3.2, §6.3), and copied into the CI
# account's private /tmp on a 3090 host (13.5). It runs the hook of the release that
# <base>/releases/hook points at, isolated, and kills it if its own 60 s bound
# fails (a refusal: any non-zero exit refuses the job).
# the release: setup writes this host's <data>/ci-runners into this line when it installs
# the hook (decision 13.5), so that a copy in /tmp/ci-runners-hooks runs the same release
base=$(/usr/bin/dirname "$(/usr/bin/dirname "$(/usr/bin/readlink -f "$0")")")  # @CI_BASE@
root=$(/usr/bin/readlink -f "$base/releases/hook") || root=
if [ -z "$root" ] || [ ! -d "$root/ci_runners" ]; then
  echo "ci-runners: refused error policy=none"
  exit 1
fi
# the interpreter: setup writes the inventory's python into this line when it installs
# the hook (design §3.2); the environment never chooses it, since a job's could
py=/usr/bin/python3  # @CI_PYTHON@
# the host's etc (the policy, the snapshot, the inventory): setup writes it into this line
# (C2c), so the hook reads them where setup put them, never where a job's environment says
etc=  # @CI_ETC@
# the decision's environment (C2c): what the hook reads (ci_runners.hook.ENV) where it is set,
# the account's HOME and a fixed PATH, nothing else: no LD_*, PYTHON* or CI_RUNNER* of a job's
set --
for v in GITHUB_ACTOR GITHUB_EVENT_NAME GITHUB_EVENT_PATH GITHUB_REF_PROTECTED GITHUB_REPOSITORY \
    GITHUB_REPOSITORY_ID GITHUB_RUN_ID GITHUB_TRIGGERING_ACTOR RUNNER_NAME HOME; do
  eval "[ -z \"\${$v+x}\" ] || set -- \"\$@\" \"$v=\${$v}\""
done
exec /usr/bin/env -i PATH=/usr/bin:/bin "$@" /usr/bin/timeout -k 5 65 "$py" -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from ci_runners.hook import main; sys.exit(main(sys.argv[1:]))' "$root" job-started "$etc"
