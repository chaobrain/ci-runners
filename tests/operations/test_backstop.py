"""brainspike's nightly backstop on 3090b (design decision 15.3): setup/maintenance/nightly/
nightly-backstop.sh and its units. The gate itself (``merge_gate.py --backstop``) is
brainspike's and tested there; here a fake gate records how the script runs it."""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
WS = ROOT / 'setup' / 'maintenance' / 'nightly'
SCRIPT = WS / 'nightly-backstop.sh'

# the fake gate: what it was run with, then the status in ./exit (0 if none)
GATE = '''import json, os, sys
keys = ('GH_TOKEN', 'GITHUB_TOKEN', 'GITHUB_REPOSITORY', 'GITHUB_ACTIONS', 'GITHUB_OUTPUT',
        'GITHUB_STEP_SUMMARY', 'MERGE_GATE_NET_WRAP', 'WRAPPED')
with open(os.environ['RECORD'], 'w') as f:
    json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'isolated': sys.flags.isolated,
               'env': {k: os.environ.get(k) for k in keys}}, f)
print('nightly backstop: fake')
sys.exit(int(open('exit').read()) if os.path.exists('exit') else 0)
'''


class Script(unittest.TestCase):
    """The login read from gh for this run only and passed to the gate in its environment;
    the gate run with -I in the checkout, under the wrapper; nothing run on bad arguments."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.t = pathlib.Path(self.tmp.name)
        self.bin = t / 'bin'
        self.bin.mkdir()
        self.gh('echo tok-xyz')
        self.checkout = t / 'brainspike'
        (self.checkout / 'scripts').mkdir(parents=True)
        (self.checkout / 'scripts' / 'merge_gate.py').write_text(GATE)
        self.wrap = t / 'wrap'
        self.wrap.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {t}/wrap.args\nWRAPPED=1 exec "$@"\n')
        self.wrap.chmod(0o755)
        self.record = t / 'record.json'

    def gh(self, body: str):
        f = self.bin / 'gh'
        f.write_text(f'#!/bin/sh\necho "$*" >> {self.t}/gh.calls\n[ "$*" = "auth token" ] || exit 9\n{body}\n')
        f.chmod(0o755)

    def run_script(self, *args, **extra):
        env = {'PATH': f'{self.bin}:/usr/bin:/bin', 'HOME': self.tmp.name, 'LANG': 'C.UTF-8',
               'RECORD': str(self.record), **extra}
        return subprocess.run([str(SCRIPT), *args], env=env, cwd=self.tmp.name, capture_output=True, text=True,
                              timeout=60)

    def args(self):
        return str(self.checkout), sys.executable, str(self.wrap)

    def test_the_gate_runs_in_the_checkout_under_the_wrapper_with_the_login(self):
        r = self.run_script(*self.args(), GITHUB_TOKEN='job-token', GITHUB_REPOSITORY='someone/else',
                            GITHUB_ACTIONS='true', GITHUB_OUTPUT='/dev/null', GITHUB_STEP_SUMMARY='/dev/null',
                            MERGE_GATE_NET_WRAP='none')
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = json.loads(self.record.read_text())
        self.assertEqual(rec['argv'], ['--backstop'])
        self.assertEqual(pathlib.Path(rec['cwd']).resolve(), self.checkout.resolve())
        self.assertEqual(rec['isolated'], 1)
        # the login, and nothing of the user manager's that the gate would read first
        self.assertEqual(rec['env'], {'GH_TOKEN': 'tok-xyz', 'GITHUB_TOKEN': None, 'GITHUB_REPOSITORY': None,
                                      'GITHUB_ACTIONS': None, 'GITHUB_OUTPUT': None, 'GITHUB_STEP_SUMMARY': None,
                                      'MERGE_GATE_NET_WRAP': None, 'WRAPPED': '1'})
        wrapped = (self.t / 'wrap.args').read_text().splitlines()
        self.assertEqual(wrapped, [sys.executable, '-I', 'scripts/merge_gate.py', '--backstop'])
        for text in (r.stdout, r.stderr, '\n'.join(wrapped)):  # never on a command line or printed
            self.assertNotIn('tok-xyz', text)
        self.assertIn('nightly backstop: fake', r.stdout)
        self.assertEqual((self.t / 'gh.calls').read_text(), 'auth token\n')  # read once, this run

    def test_report_only_is_passed_on_and_the_commit_printed(self):
        g = ['git', '-C', str(self.checkout), '-c', 'user.name=t', '-c', 'user.email=t@t']
        subprocess.run([*g, 'init', '-q', '-b', 'main'], check=True)
        subprocess.run([*g, 'add', '-A'], check=True)
        subprocess.run([*g, 'commit', '-q', '-m', 'x'], check=True)
        sha = subprocess.run([*g, 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
        r = self.run_script('--report-only', *self.args())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(self.record.read_text())['argv'], ['--backstop', '--report-only'])
        self.assertIn(f'nightly-backstop.sh: main at {sha[:12]}', r.stdout)

    def test_the_status_is_the_gates(self):
        (self.checkout / 'exit').write_text('2')  # a GitHub error
        self.assertEqual(self.run_script(*self.args()).returncode, 2)

    def test_bad_arguments_read_no_login_and_run_nothing(self):
        c, py, w = self.args()
        (self.t / 'empty').mkdir()
        (self.t / 'plain').write_text('')
        odd = self.t / 'brain spike;x'  # a checkout that exists, at a path of more than one word
        (odd / 'scripts').mkdir(parents=True)
        (odd / 'scripts' / 'merge_gate.py').write_text(GATE)
        for args in ((), (c, py), (c, py, w, w), ('--report-only',), (c, '--report-only', py, w),
                     ('brainspike', py, w), (c, 'python3', w), (c, py, 'proxy'), ('-F/x', py, w),
                     (c + ' x', py, w), (c + ';x', py, w), (c, py, w + '$(id)'), (str(odd), py, w),
                     (str(self.t / 'empty'), py, w), (c, str(self.t / 'plain'), w), (c, py, str(self.t / 'empty')),
                     (c, py, str(self.t / 'none'))):
            r = self.run_script(*args)
            self.assertEqual(r.returncode, 2, args)
            self.assertFalse((self.t / 'gh.calls').exists(), args)
            self.assertFalse(self.record.exists(), args)

    def test_no_login_runs_nothing(self):
        for body in ('exit 1', 'exit 0'):  # gh fails; gh prints nothing
            self.gh(body)
            r = self.run_script(*self.args())
            self.assertNotEqual(r.returncode, 0, body)
            self.assertFalse(self.record.exists(), body)


def unit(name: str, directory: pathlib.Path = WS) -> list[tuple[str, str]]:
    """A unit file's settings, in order, without comments or sections."""
    out = []
    for line in (directory / name).read_text().splitlines():
        if line and not line.startswith(('#', '[')):
            k, _, v = line.partition('=')
            out.append((k, v))
    return out


def ticks(spec: str) -> list[datetime.time]:
    """The daily times of an ``OnCalendar`` of the form ``*-*-* H:M:S UTC``, each field a
    comma list of numbers (all this timer uses)."""
    m = re.fullmatch(r'\*-\*-\* ([\d,]+):([\d,]+):([\d,]+) UTC', spec)
    assert m, spec
    h, mi, s = ([int(x) for x in g.split(',')] for g in m.groups())
    return [datetime.time(a, b, c) for a in h for b in mi for c in s]


class Units(unittest.TestCase):
    def test_the_timer_ticks_every_quarter_hour_inside_the_window(self):
        """In UTC, exact, from 21:00; each tick starts inside brainspike's backstop window
        (20:50 to 23:00 UTC, inclusive), so the last is 22:59, not 23:00."""
        st = unit('ci-nightly-backstop.timer')
        got = sorted(t for k, v in st if k == 'OnCalendar' for t in ticks(v))
        want = [datetime.time(h, m) for h in (21, 22) for m in (0, 15, 30, 45)] + [datetime.time(22, 59)]
        self.assertEqual(got, want)
        self.assertTrue(all(datetime.time(20, 50) <= t < datetime.time(23, 0) for t in got))
        self.assertIn(('AccuracySec', '1s'), st)
        keys = {k for k, _ in st}
        self.assertFalse(keys & {'RandomizedDelaySec', 'Persistent', 'OnBootSec', 'OnUnitActiveSec'}, keys)
        self.assertIn(('WantedBy', 'timers.target'), st)

    def test_the_service_runs_the_script_with_its_placeholders(self):
        st = dict(unit('ci-nightly-backstop.service'))
        self.assertEqual(st['ExecStart'], '@RELEASE@/setup/maintenance/nightly/nightly-backstop.sh @CHECKOUT@ @PYTHON@ @NET_WRAP@')
        self.assertEqual((st['Type'], st['NoNewPrivileges'], st['TimeoutStartSec']), ('oneshot', 'yes', '5min'))
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == '__main__':
    unittest.main()
