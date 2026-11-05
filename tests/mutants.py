"""Mutants of the lines the tests guard (AGENTS.md, working agreement 4).

Each mutant replaces one exact snippet in a copy of the tree and runs only the tests
that should kill it; a mutant that passes them survives and fails this script. Run it
with ``python tests/mutants.py`` from the repository root (CI runs it on every PR).
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

POL = 'ci_runners/policy.py'
REM = 'ci_runners/remote.py'
INV = 'ci_runners/inventory.py'
TP = 'tests.test_policy'
TR = 'tests.test_remote_env'
TI = 'tests.test_inventory'

#: (file, snippet, replacement, tests that kill it)
MUTANTS = [
    # the hook policy
    (POL, "    if cls not in CLASSES:", "    if cls is None:", [f'{TP}.Refused.test_an_unknown_runner_class']),
    (POL, "    if name == SELF:\n        return no('self')", "    if name == 'x/y':\n        return no('self')",
     [f'{TP}.Refused.test_an_unlisted_repository_and_this_one']),
    (POL, " or int(rid) != repo.id:", ":", [f'{TP}.Refused.test_the_repository_id_is_checked_against_the_name']),
    (POL, "isinstance(r, dict) and type(r.get('id')) is int and r['id'] == repo.id",
     "isinstance(r, dict) and r['id'] == repo.id", [f'{TP}.Refused.test_the_repository_id_is_checked_against_the_name']),
    (POL, "and r['id'] == repo.id\n", "\n", [f'{TP}.Refused.test_the_repository_id_is_checked_against_the_name']),
    (POL, "if isinstance(s, str) and pattern.match(s):", "if isinstance(s, str):",
     [f'{TP}.Refused.test_the_repository_id_is_checked_against_the_name']),
    (POL, "    elif event_name != 'schedule':", "    elif False:",
     [f'{TP}.Refused.test_a_payload_without_a_repository_or_not_an_object']),
    (POL, "    if repo.paused:", "    if False:", [f'{TP}.Refused.test_a_paused_repository']),
    (POL, "    if public and cls not in PUBLIC_CLASSES:", "    if False:",
     [f'{TP}.Refused.test_a_public_repository_on_a_part_runner']),
    (POL, "PUBLIC_EVENTS if public else PRIVATE_EVENTS | repo.extra_events", "PRIVATE_EVENTS | repo.extra_events",
     [f'{TP}.Refused.test_other_events']),
    (POL, "PUBLIC_EVENTS if public else PRIVATE_EVENTS | repo.extra_events", "PUBLIC_EVENTS if public else PRIVATE_EVENTS",
     [f'{TP}.Refused.test_pull_request_target_is_refused_unless_listed_and_then_still_not_from_a_fork']),
    (POL, "for side in ('head', 'base'):", "for side in ('base',):", [f'{TP}.Refused.test_fork_pull_requests']),
    (POL, "for side in ('head', 'base'):", "for side in ('head',):", [f'{TP}.Refused.test_fork_pull_requests']),
    (POL, "    if 'pull_request' in event or event_name.startswith('pull_request'):\n        pr =",
     "    if event_name == 'pull_request':\n        pr =",
     [f'{TP}.Refused.test_pull_request_target_is_refused_unless_listed_and_then_still_not_from_a_fork']),
    (POL, "    if event_name == 'workflow_run':", "    if False:", [f'{TP}.Refused.test_a_workflow_run_of_a_fork_or_an_outsider']),
    (POL, "if public or event_name not in BOT_EVENTS:", "if event_name not in BOT_EVENTS:", [f'{TP}.Refused.test_the_bot']),
    (POL, "if public or event_name not in BOT_EVENTS:", "if public:", [f'{TP}.Refused.test_the_bot']),
    (POL, "        elif login not in policy.actors:", "        elif False:", [f'{TP}.Refused.test_unlisted_actors']),
    (POL, ", _ascii_lower(env.get('GITHUB_TRIGGERING_ACTOR'), _LOGIN)]", "]", [f'{TP}.Refused.test_unlisted_actors']),
    (POL, "        out.append(_login(event['sender'], 'login'))", "        pass", [f'{TP}.Refused.test_unlisted_actors']),
    (POL, "        out.append(_login(event.get('pusher'), 'name'))", "        pass", [f'{TP}.Refused.test_unlisted_actors']),
    (POL, "        out.append(_login(pr.get('user') if isinstance(pr, dict) else None, 'login'))", "        pass",
     [f'{TP}.Refused.test_unlisted_actors']),
    (POL, "            out.append(_login(wr.get(key) if isinstance(wr, dict) else None, 'login'))", "            pass",
     [f'{TP}.Refused.test_a_workflow_run_of_a_fork_or_an_outsider']),
    (POL, "isinstance(e, str) and e in EXTRA_EVENTS for e in extra", "isinstance(e, str) and _EVENT.match(e) for e in extra",
     [f'{TP}.Load.test_malformed_policies']),
    (POL, "    if 'repository' in event:\n", "    if 'repository' in event and event_name != 'schedule':\n",
     [f'{TP}.Refused.test_a_payload_without_a_repository_or_not_an_object']),
    (POL, "    if 'pull_request' in event or name.startswith('pull_request'):\n        pr = event.get('pull_request')\n        out",
     "    if name == 'pull_request':\n        pr = event.get('pull_request')\n        out",
     [f'{TP}.Refused.test_pull_request_target_is_refused_unless_listed_and_then_still_not_from_a_fork']),
    (POL, "if login is None or login.endswith('[bot]'):", "if login is None:", [f'{TP}.Load.test_malformed_policies']),
    (POL, "        if name == SELF:\n            raise", "        if False:\n            raise",
     [f'{TP}.Load.test_malformed_policies']),
    (POL, "if extra and vis == 'public':", "if False:", [f'{TP}.Load.test_malformed_policies']),
    # the env allowlist
    (REM, "up.startswith(REFUSED_PREFIXES) or ", "", [f'{TR}.RemoteEnv.test_refused', f'{TR}.RemoteEnv.test_only_the_allowlist_passes']),
    (REM, "any(p in up for p in REFUSED_PARTS)", "any(p in name for p in REFUSED_PARTS)", [f'{TR}.RemoteEnv.test_refused']),
    (REM, " or up in REFUSED_NAMES", "", [f'{TR}.RemoteEnv.test_refused']),
    (REM, " and '\\0' not in v", "", [f'{TR}.RemoteEnv.test_only_the_allowlist_passes']),
    (REM, " | {k for k in environ if isinstance(k, str) and k.startswith('CI_')}", "",
     [f'{TR}.RemoteEnv.test_only_the_allowlist_passes']),
    (REM, "out = {**{k: fixed[k] for k in FIXED}", "out = {**{k: environ.get(k, fixed[k]) for k in FIXED}",
     [f'{TR}.RemoteEnv.test_fixed_values_come_from_the_inventory']),
    (REM, "'CUDA_VISIBLE_DEVICES': cuda}", "'CUDA_VISIBLE_DEVICES': environ.get('CUDA_VISIBLE_DEVICES', cuda)}",
     [f'{TR}.RemoteEnv.test_the_card_is_the_drivers']),
    (REM, "    fixed_names = set(FIXED) | {'CUDA_VISIBLE_DEVICES'}", "    fixed_names = set(FIXED)",
     [f'{TR}.RemoteEnv.test_the_card_is_the_drivers']),
    (REM, " and not _TOKEN_VALUE.search(v)", "", [f'{TR}.RemoteEnv.test_token_like_values_never_leave']),
    (REM, "        if not refused(k) and isinstance(v, str)", "        if isinstance(v, str)",
     [f'{TR}.RemoteEnv.test_only_the_allowlist_passes']),
    # the inventory: schema and lint
    (INV, "    if doc.get('schema') != SCHEMA:", "    if False:", [f'{TI}.Load.test_an_unknown_schema_is_refused']),
    (INV, "            if k not in PUBLIC_KEYS:", "            if True:", [f'{TI}.Deny.test_deny_tokens']),
    (INV, "len(t) >= MIN_TOKEN and", "len(t) >= 1 and", [f'{TI}.Deny.test_deny_tokens']),
    (INV, "spans[i:i + MAX_PARTS]", "spans[i:i + 1]", [f'{TI}.Scan.test_denied_tokens_anywhere_in_a_word']),
    (INV, " or _PUBLIC_IP.match(m.group())", "", [f'{TI}.Scan.test_patterns']),
    (INV, "(not all(int(g) <= 255 for g in m.groups()) or ", "(", [f'{TI}.Scan.test_patterns']),
    (INV, "(?!runner(?![\\w.-]))", "(?!runner\\b)", [f'{TI}.Scan.test_patterns']),
    (INV, "(?!\\w|\\.\\d)')", "(?![\\w.])')", [f'{TI}.Scan.test_patterns']),
    (INV, "                    t = t[:spans[MAX_PARTS - 1][1]]", "                    pass",
     [f'{TI}.Deny.test_tokens_split_as_the_lint_reads_words']),
    (INV, "            for m in _WORD.finditer(s):  # split where", "            for m in re.finditer(r'[^\\s,]+', s):  # split where",
     [f'{TI}.Deny.test_tokens_split_as_the_lint_reads_words']),
    (INV, "Finding(f'<the name of tracked file {n}>', 0, f.kind)", "Finding(rel, 0, f.kind)",
     [f'{TI}.Scan.test_lint_scans_contents_and_names_and_skips_binaries']),
    (INV, "        if b'\\0' in data[:8192]:\n            continue", "",
     [f'{TI}.Scan.test_lint_scans_contents_and_names_and_skips_binaries']),
    (INV, "        out += [Finding(f'<the name of tracked file {n}>', 0, f.kind) for f in scan(rel, deny)]\n", "",
     [f'{TI}.Scan.test_lint_scans_contents_and_names_and_skips_binaries']),
    (INV, "return hashlib.sha256(_SALT + normalise(token).encode()).hexdigest()",
     "return hashlib.sha256(normalise(token).encode()).hexdigest()", [f'{TI}.Deny.test_hash_is_salted_and_normalised']),
    (INV, "    return token.strip(_SEP).lower()", "    return token.lower()", [f'{TI}.Deny.test_hash_is_salted_and_normalised']),
]


def run(tree: pathlib.Path, tests: list[str]) -> int:
    # -B: a stale .pyc of an earlier mutant of the same size and second would be used
    return subprocess.run([sys.executable, '-B', '-m', 'unittest', '-q', *tests], cwd=tree, capture_output=True,
                          timeout=300).returncode


def main() -> int:
    survived = 0
    with tempfile.TemporaryDirectory() as d:
        tree = pathlib.Path(d) / 'tree'
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        every = sorted({t for *_, ts in MUTANTS for t in ts})
        if run(tree, every):
            print('the unmutated tree fails its tests', file=sys.stderr)
            return 2
        for n, (rel, old, new, tests) in enumerate(MUTANTS, 1):
            path = tree / rel
            src = path.read_text()
            if src.count(old) != 1:
                print(f'mutant {n}: the snippet occurs {src.count(old)} times in {rel}', file=sys.stderr)
                return 2
            path.write_text(src.replace(old, new))
            try:
                killed = run(tree, tests) != 0
            finally:
                path.write_text(src)
            survived += not killed
            print(f'{"killed  " if killed else "SURVIVED"} {n:3d} {rel}: {old.strip()[:70]!r}')
    print(f'{len(MUTANTS) - survived}/{len(MUTANTS)} mutants killed')
    return 1 if survived else 0


if __name__ == '__main__':
    sys.exit(main())
