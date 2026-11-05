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
LCK = 'ci_runners/locks.py'
HOK = 'ci_runners/hook.py'
SET = 'ci_runners/setup.py'
PRX = 'ci_runners/proxy.py'
CLA = 'ci_runners/claim.py'
OUT = 'ci_runners/outcome.py'
GPU = 'ci_runners/gpus.py'
PLA = 'ci_runners/place.py'
SLO = 'ci_runners/slots.py'
TP = 'tests.test_policy'
TR = 'tests.test_remote_env'
TI = 'tests.test_inventory'
TX = 'tests.test_interop'
TH = 'tests.test_hook'
TU = 'tests.test_setup'
TY = 'tests.test_proxy'
TC = 'tests.test_claim'
TO = 'tests.test_outcome'
TG = 'tests.test_gpus'
TL = 'tests.test_place'
TS = 'tests.test_slots'

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
    # the claim
    (CLA, "frozenset() if hosted else frozenset(", "frozenset(", [f'{TC}.Decide.test_on_a_hosted_runner_only_the_bot_counts']),
    (CLA, "for a in actors) - {bot}", "for a in actors)", [f'{TC}.Decide.test_the_bot_listed_as_an_actor_is_still_the_bot']),
    (CLA, "max(counted, key=lambda s: s.time)", "counted[0]", [f'{TC}.Decide.test_only_the_newest_counted_status_decides']),
    (CLA, "if newest.state == 'failure' and local:", "if newest.state == 'failure':",
     [f'{TC}.Decide.test_a_local_red_skips_but_a_runner_red_runs']),
    (CLA, "now - newest.time < lapse_minutes * 60", "now - newest.time <= lapse_minutes * 60",
     [f'{TC}.Decide.test_a_live_local_claim_skips_until_it_lapses']),
    (CLA, "if s.creator.lower() == bot or s.creator", "if s.creator",
     [f'{TC}.Decide.test_green_by_the_bot_or_a_member_skips']),
    (CLA, "if dt.tzinfo is not None else None", "", [f'{TC}.Parse.test_parse']),
    # outcomes and hints
    (OUT, "    if e.stopped:\n        return Outcome('stopped'", "    if False:\n        return Outcome('stopped'",
     [f'{TO}.Classify.test_precedence']),
    (OUT, "e.boot_start and e.boot_end and e.boot_start != e.boot_end", "e.boot_end and e.boot_start != e.boot_end",
     [f'{TO}.Classify.test_each_outcome_from_its_own_evidence']),
    (OUT, "    if e.early_stop:", "    if False:", [f'{TO}.Classify.test_each_outcome_from_its_own_evidence']),
    (OUT, "return [k for k in kills if k.bus in cards and any(", "return [k for k in kills if any(",
     [f'{TO}.Watchdog.test_ours_needs_our_card_and_a_recorded_pid']),
    (OUT, "(not end or m.group(1) <= end)", "True", [f'{TO}.Watchdog.test_kill_lines_only']),
    (OUT, "(not start or m.group(1) >= start)", "True", [f'{TO}.Watchdog.test_kill_lines_only']),
    (OUT, "        s = parts[-2] if len(parts) >= 2 else s", "        s = parts[-1]", [f'{TO}.Watchdog.test_bus_key']),
    (OUT, "    if e.oom_kills > 0 or journal_oom(e.journal, e.unit):", "    if journal_oom(e.journal, e.unit):",
     [f'{TO}.Classify.test_each_outcome_from_its_own_evidence']),
    (OUT, "(not unit or unit in line or '.scope' not in line)", "(not unit or unit in line)", [f'{TO}.Oom.test_journal']),
    (OUT, "m.group(1).endswith('/' + unit)", "m.group(1).endswith(unit)", [f'{TO}.Oom.test_journal']),
    (OUT, "if k == 'oom_kill' and", "if k.startswith('oom') and", [f'{TO}.Oom.test_memory_events']),
    (OUT, "e.yield_marker > e.start_epoch", "e.yield_marker >= e.start_epoch",
     [f'{TO}.Classify.test_each_outcome_from_its_own_evidence']),
    (OUT, "    if e.rc is None:\n", "    if False:\n", [f'{TO}.Classify.test_each_outcome_from_its_own_evidence']),
    (OUT, "    if e.timed_out:", "    if False:", [f'{TO}.Classify.test_each_outcome_from_its_own_evidence']),
    (OUT, "if kind in FINAL or attempt > retries:", "if kind in FINAL:", [f'{TO}.Hint.test_at_most_one_retry']),
    (OUT, "if kind in FINAL or attempt > retries:", "if kind in FINAL or attempt >= retries:", [f'{TO}.Hint.test_hints']),
    (OUT, "    if kind == 'reboot' and remote:", "    if False:", [f'{TO}.Hint.test_hints']),
    (OUT, "Hint('retry', resume=True, where='more-memory')", "Hint('retry', where='more-memory')", [f'{TO}.Hint.test_hints']),
    (OUT, "if cool_card else Hint('fallback')", "", [f'{TO}.Hint.test_hints']),
    # GPUs
    (GPU, "if i in free and free[i] >= need_mib]", "if free.get(i, 0) >= need_mib]", [f'{TG}.Gpus.test_free_memory_and_rank']),
    (GPU, "if leased.get(i, 0) < max_parts]", "]", [f'{TG}.Gpus.test_lease']),
    (GPU, "return ok[:n] if len(ok) >= n else None", "return ok[:n]", [f'{TG}.Gpus.test_lease']),
    (GPU, "for i in cards if i in free}", "for i in free}", [f'{TG}.Gpus.test_lease']),
    # place
    (PLA, "    if not p.proxy:", "    if False:", [f'{TL}.Place.test_a_host_that_fails_a_probe_gets_no_part']),
    (PLA, "    if p.reserved:", "    if False:", [f'{TL}.Place.test_a_host_that_fails_a_probe_gets_no_part']),
    (PLA, "    if p.quarantined:", "    if False:", [f'{TL}.Place.test_a_host_that_fails_a_probe_gets_no_part']),
    (PLA, "return part.cls == 'gpu' and part.gpus == 1 and ", "return ",
     [f'{TL}.Place.test_h100_takes_gpu_parts_with_one_card_only']),
    (PLA, "    if POOL[part.cls] not in p.runners:", "    if False:",
     [f'{TL}.Place.test_a_host_that_fails_a_probe_gets_no_part']),
    (PLA, "return c.healthy and not c.others and c.free_mib >= need_mib and c.temp_c < max_temp_c",
     "return c.healthy and not c.others and c.free_mib >= need_mib",
     [f'{TL}.Place.test_each_h100_part_gets_its_own_coolest_fitting_card', f'{TL}.Place.test_a_3090_needs_enough_fitting_cards']),
    (PLA, "return c.healthy and not c.others and c.free_mib", "return not c.others and c.free_mib",
     [f'{TL}.Place.test_each_h100_part_gets_its_own_coolest_fitting_card']),
    (PLA, "            taken.add(card.index)", "            pass", [f'{TL}.Place.test_each_h100_part_gets_its_own_coolest_fitting_card']),
    (PLA, " and f'{REMOTE_POOL}-gpu{c.index}' in p.runners", "",
     [f'{TL}.Place.test_each_h100_part_gets_its_own_coolest_fitting_card']),
    (PLA, "key=lambda c: (c.temp_c, -c.free_mib, c.index)", "key=lambda c: (c.temp_c, c.index)",
     [f'{TL}.Place.test_each_h100_part_gets_its_own_coolest_fitting_card']),
    (PLA, "load[h] < probes[h].slots", "load[h] <= probes[h].slots",
     [f'{TL}.Place.test_load_spreads_to_a_host_with_room_and_waits_on_the_first_when_none_has']),
    (PLA, "        load[host] += 1\n", "",
     [f'{TL}.Place.test_load_spreads_to_a_host_with_room_and_waits_on_the_first_when_none_has']),
    (PLA, "if h not in exclude]", "]", [f'{TL}.Place.test_excluded_hosts']),
    (PLA, "math.ceil(2 * max(0.0, estimate_minutes) * max(1.0, slowdown) + 30)",
     "int(2 * max(0.0, estimate_minutes) * max(1.0, slowdown) + 30)", [f'{TL}.Place.test_timeout']),
    (PLA, "* max(1.0, slowdown) + 30", "* slowdown + 30", [f'{TL}.Place.test_timeout']),
    (PLA, "if (p['class'] == 'gpu') != (gpus > 0):", "if False:", [f'{TL}.Parse.test_malformed']),
    # the fair queue
    (SLO, "held.get(p, 0) >= caps.get(p, 0)", "False", [f'{TS}.Grants.test_a_project_at_its_cap_is_skipped']),
    (SLO, "key = (held.get(p, 0), t.seq)", "key = (0, t.seq)", [f'{TS}.Grants.test_the_project_holding_fewest_goes_first']),
    (SLO, "        held[p] = held.get(p, 0) + 1\n", "", [f'{TS}.Grants.test_a_project_at_its_cap_is_skipped']),
    (SLO, "p = t.project or default_project\n            if", "p = t.project\n            if",
     [f'{TS}.Grants.test_tickets_without_a_project_are_the_default_projects']),
    (SLO, "return waiting >= max_waiting", "return waiting > max_waiting", [f'{TS}.Grants.test_queue_full']),
    # the host lock and the slots, against brainspike's originals (acceptance 2)
    (LCK, "                    fcntl.flock(ts, fcntl.LOCK_UN)\n                    os.close(ts)\n                    self._fds.remove(ts)\n", "",
     [f'{TX}.LockInterop.test_every_mode_pair_both_ways']),
    (LCK, "self._flock(fd, fcntl.LOCK_SH if want == 's' else fcntl.LOCK_EX, t0)", "self._flock(fd, fcntl.LOCK_SH, t0)",
     [f'{TX}.LockInterop.test_every_mode_pair_both_ways']),
    (LCK, "                    if mode == 's':  # a normal shared holder", "                    if False:",
     [f'{TX}.LockInterop.test_light_enters_beside_a_normal_shared_holder_only']),
    (LCK, "            if mode == 'l' and self._normal_shared_in():", "            if False:",
     [f'{TX}.LockInterop.test_light_enters_beside_a_normal_shared_holder_only']),
    (LCK, "            elif self.held == 'x' or want == 's':", "            elif want == 's':",
     [f'{TX}.LockInterop.test_nesting_covers_cross_implementations']),
    (LCK, "            if not self.cover_live():", "            if False:", [f'{TX}.LockInterop.test_a_stale_cover_takes_the_lock']),
    (LCK, "if left <= 0 or not _wait_in_queue(fd, op, left):", "if not _wait_in_queue(fd, op, 1e6):",
     [f'{TX}.LockInterop.test_a_timeout']),
    (SLO, "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)", "fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)",
     [f'{TX}.SlotInterop.test_together_never_more_than_the_slot_count', f'{TS}.Queue.test_a_taken_slot_is_held_exclusively']),
    (SLO, "                        return True\n    except (OSError", "                        pass\n    except (OSError",
     [f'{TS}.Queue.test_flocked_reads_holders']),
    # the hook, setup and the proxy check
    (HOK, "    return (0 if d.accepted else 1), policy.line(d, digest)", "    return 0, policy.line(d, digest)",
     [f'{TH}.Started.test_accept_and_refuse']),
    (HOK, "except (OSError, ValueError, RecursionError) as e:", "except (OSError, ValueError) as e:",
     [f'{TH}.Started.test_errors_refuse']),
    (HOK, "    if len(data) > limit:", "    if False:", [f'{TH}.Started.test_an_oversized_event_is_refused']),
    (SET, "    if plan.text() != approved:", "    if False:",
     [f'{TU}.Setup.test_apply_refuses_a_plan_that_is_not_the_fresh_one']),
    (SET, "    if secret:\n", "    if False:\n", [f'{TU}.Setup.test_plan_apply_and_a_second_run']),
    (SET, "            os.chmod(a['path'], a['mode'])\n        elif k == 'file':", "            pass\n        elif k == 'file':",
     [f'{TU}.Setup.test_plan_apply_and_a_second_run']),
    (SET, "    out.pop('log', None)\n    return out", "    return src", [f'{TU}.Setup.test_plan_apply_and_a_second_run']),
    (PRX, "    if failures < threshold:", "    if failures <= threshold:", [f'{TY}.Step.test_three_failures_switch_to_the_next_node']),
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
    try:  # no pipes: a holder a mutant leaves behind must not keep the run waiting
        return subprocess.run([sys.executable, '-B', '-m', 'unittest', '-q', *tests], cwd=tree,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=300).returncode
    except subprocess.TimeoutExpired:
        return 124  # a hang fails the tests


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
