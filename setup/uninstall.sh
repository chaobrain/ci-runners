#!/bin/sh
# setup/uninstall.sh ROLE: stop and disable this host's units, one by one, and remove
# the unit files that setup wrote (as state/setup.json records them) (design §5.2). If
# any unit is still active afterwards, nothing is removed. Runner directories, etc/
# and state/ stay: the lead deregisters the runners (config.sh remove, or the org's
# runner settings) and deletes etc/.
set -eu
[ $# -eq 1 ] || { echo "usage: $0 ROLE" >&2; exit 2; }
ci_run=$HOME/data/ci-runners/releases/hook/bin/ci-run
role=$("$ci_run" role)
[ "$role" = "$1" ] || { echo "uninstall.sh: this host's inventory is for $role, not $1" >&2; exit 2; }
units="$("$ci_run" runners | awk '{print "ci-runner@" $1 ".service"}') ci-proxy-check.timer ci-reaper.timer ci-proxy.service"
for u in $units; do
  systemctl --user disable --now "$u" >/dev/null 2>&1 || true  # one missing unit stops none of the others
done
left=
for u in $units; do
  systemctl --user is-active --quiet "$u" && left="$left $u"
done
[ -z "$left" ] || { echo "uninstall.sh: still active:$left; nothing removed" >&2; exit 1; }
unit_dir=$HOME/.config/systemd/user
/usr/bin/python3 -I - "$HOME/data/ci-runners/state/setup.json" "$unit_dir" <<'PY'
import json, os, sys
state, unit_dir = sys.argv[1], sys.argv[2]
with open(state) as f:
    made = [m for a in json.load(f)['applied'] for m in a['made']]
for p in sorted({m['path'] for m in made if m['kind'] == 'file' and os.path.dirname(m['path']) == unit_dir}):
    if os.path.lexists(p):
        os.unlink(p)
        print(f'uninstall.sh: removed {p}')
PY
systemctl --user daemon-reload
echo "uninstall.sh: units removed; runner directories, etc/ and state/ are left for the lead"
