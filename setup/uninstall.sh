#!/bin/sh
# setup/uninstall.sh ROLE: stop and disable this host's units and remove their files
# (design §5.2). Runner directories, etc/ and state/ stay: the lead deregisters the
# runners (config.sh remove, or the org's runner settings) and deletes etc/.
set -eu
[ $# -eq 1 ] || { echo "usage: $0 ROLE" >&2; exit 2; }
ci_run=$HOME/data/ci-runners/releases/hook/bin/ci-run
units=$("$ci_run" runners | awk '{print "ci-runner@" $1 ".service"}')
systemctl --user disable --now $units ci-proxy-check.timer ci-reaper.timer ci-proxy.service 2>/dev/null || true
cd "$HOME/.config/systemd/user"
rm -f ci-runner@.service ci-proxy.service ci-proxy-check.service ci-proxy-check.timer ci-reaper.service \
  ci-reaper.timer ci.slice ci-*.slice
systemctl --user daemon-reload
echo "uninstall.sh: units removed; runner directories, etc/ and state/ are left for the lead"
