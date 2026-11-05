"""Mutants of the lines the tests guard (AGENTS.md, working agreement 4).

Each mutant replaces one exact snippet in a copy of the tree and runs only the tests
that should kill it; a mutant that passes them survives and fails this script. Up to
``WORKERS`` mutants run at once, each in its own copy; tests still running after
``HANG`` seconds have hung, which kills the mutant. Run it with
``python tests/mutants.py`` from the repository root (CI runs it on every PR).
"""
from __future__ import annotations

import concurrent.futures
import os
import pathlib
import queue
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]

WORKERS = 4  # a hosted runner's cores; each worker has its own copy of the tree
HANG = 60  # seconds: the slowest guarded test takes about 9 s, so a mutant's tests still running have hung
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
PRB = 'ci_runners/probe.py'
GHB = 'ci_runners/github.py'
PRT = 'ci_runners/part.py'
RGY = 'ci_runners/registry.py'
REA = 'ci_runners/reaper.py'
REG = 'setup/register.sh'
INS = 'setup/install.sh'
UNI = 'setup/uninstall.sh'
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
TQ = 'tests.test_probe_claim'
TT = 'tests.test_part'
TE = 'tests.test_reaper'
TK = 'tests.test_scripts'

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
    (POL, "if public or not repo.bot_dispatch or event_name not in BOT_EVENTS:", "if not repo.bot_dispatch or event_name not in BOT_EVENTS:", [f'{TP}.Refused.test_the_bot']),
    (POL, "if public or not repo.bot_dispatch or event_name not in BOT_EVENTS:", "if public or not repo.bot_dispatch:", [f'{TP}.Refused.test_the_bot_dispatch_switch_is_off_by_default']),
    (POL, "if public or not repo.bot_dispatch or event_name not in BOT_EVENTS:", "if public or event_name not in BOT_EVENTS:", [f'{TP}.Refused.test_the_bot_dispatch_switch_is_off_by_default']),
    (POL, "if public or not repo.bot_dispatch or event_name not in BOT_EVENTS:", "if True:", [f'{TP}.Accepted.test_the_bot_may_dispatch_where_the_switch_is_on']),
    (POL, "        bot_dispatch = r.get('bot_dispatch', False)", "        bot_dispatch = r.get('bot_dispatch', True)", [f'{TP}.Refused.test_the_bot_dispatch_switch_is_off_by_default']),
    (POL, "        if bot_dispatch and vis == 'public':", "        if False:", [f'{TP}.Load.test_malformed_policies']),
    (POL, "        if type(bot_dispatch) is not bool:", "        if False:", [f'{TP}.Load.test_malformed_policies']),
    (POL, "        if type(bot_dispatch) is not bool:", "        if not isinstance(bot_dispatch, int):", [f'{TP}.Load.test_malformed_policies']),
    (POL, "        if type(paused) is not bool:", "        if not isinstance(paused, int):", [f'{TP}.Load.test_malformed_policies']),
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
    # review round 1 of the hook, setup, the proxy check, the reaper and the scripts
    (HOK, "    except _Timeout:\n        d = policy.Decision(False, 'timeout')", "    except _Timeout:\n        d = policy.Decision(True, 'timeout')",
     [f'{TH}.Started.test_a_hung_read_is_refused_at_the_bound']),
    (HOK, "        signal.alarm(START_BOUND)\n", "", [f'{TH}.Started.test_a_hung_read_is_refused_at_the_bound']),
    (HOK, "            code, out = 1, policy.line(policy.Decision(False, 'timeout'), '')",
     "            raise", [f'{TH}.Started.test_the_bound_outside_the_decision_still_prints_the_fixed_line']),
    (HOK, " | os.O_NONBLOCK", "", [f'{TH}.Started.test_a_planted_log_fifo_neither_blocks_nor_breaks_the_line']),
    (HOK, " | os.O_NOFOLLOW", "", [f'{TH}.Started.test_a_planted_log_fifo_neither_blocks_nor_breaks_the_line']),
    (SET, "    if h.hexdigest() != RUNNER_SHA256:", "    if False:", [f'{TU}.Setup.test_the_runner_app_is_checked_and_unpacked_whole']),
    (SET, "        if port:\n            wait_port(port)\n", "", [f'{TU}.Setup.test_the_download_waits_for_the_proxy_to_listen']),
    (SET, "            if time.monotonic() >= deadline:", "            if False:", [f'{TU}.Setup.test_a_proxy_that_never_listens_fails_within_the_bound']),
    (SET, "    if not runner_complete(tmp):", "    if False:", [f'{TU}.Setup.test_the_runner_app_is_checked_and_unpacked_whole']),
    (SET, "            if os.path.lexists(tmp):\n                os.unlink(tmp)  # a leftover", "            if False:\n                os.unlink(tmp)  # a leftover",
     [f'{TU}.Setup.test_a_planted_link_is_not_followed']),
    (SET, "    if not plan.actions:\n        print(f'apply: nothing", "    if False:\n        print(f'apply: nothing",
     [f'{TU}.Setup.test_plan_apply_and_a_second_run']),
    (SET, "        if not PROJECT_NAME.fullmatch(name) or not MEMORY.fullmatch(mem):", "        if False:",
     [f'{TU}.Setup.test_project_names_and_memory_are_single_words']),
    (PRX, "    except urllib.error.HTTPError:\n        return True", "    except urllib.error.HTTPError:\n        return False",
     [f'{TY}.Probe.test_any_http_answer_means_the_proxy_works']),
    (REA, "                    t = max(t, os.lstat(os.path.join(top, n)).st_mtime)", "                    pass",
     [f'{TE}.Reap.test_a_run_counts_by_its_newest_file']),
    (REA, "RUN_DEPTH = 3", "RUN_DEPTH = 1", [f'{TE}.Reap.test_a_run_counts_by_its_newest_file']),
    (REA, "if os.path.basename(p).startswith('hook-')", "if True", [f'{TE}.Reap.test_dated_logs_go_after_14_days_and_nothing_else_there']),
    (REA, "    if os.path.isdir(p) and not os.path.islink(p):\n        shutil.rmtree", "    if os.path.isdir(p):\n        shutil.rmtree",
     [f'{TE}.Reap.test_a_link_is_removed_not_followed']),
    (REG, "exec ./config.sh \"$@\"' \\\n    config.sh --unattended", "exec ./config.sh --token \"$ACTIONS_RUNNER_INPUT_TOKEN\" \"$@\"' \\\n    config.sh --unattended",
     [f'{TK}.Register.test_the_token_reaches_config_only_through_its_environment']),
    (REG, "env -i PATH=", "env PATH=", [f'{TK}.Register.test_the_token_reaches_config_only_through_its_environment']),
    (REG, "[ -z \"$running\" ] ||", "true ||", [f'{TK}.Register.test_refusals']),
    (REG, " && [ -x \"$dir/bin/Runner.Listener\" ]", "", [f'{TK}.Register.test_refusals']),
    (INS, "  [ \"$got\" = \"$sha\" ] || die", "  true || die", [f'{TK}.Install.test_an_annotated_tag_resolves_to_its_commit']),
    (INS, "awk -v r=\"refs/tags/$tag^{}\" '$2 == r {print $1}'", "awk '{print $1}' | head -1",
     [f'{TK}.Install.test_an_annotated_tag_resolves_to_its_commit']),
    (INS, "        [ \"$top\" = \"$tag\" ] || die", "        true || die", [f'{TK}.Install.test_the_hook_moves_only_forward']),
    (INS, "  [ -d \"$rel/$id\" ] && [ ! -L \"$rel/$id\" ] || die", "  [ -d \"$rel/$id\" ] || die",
     [f'{TK}.Install.test_an_existing_release_must_be_the_real_one']),
    (INS, "  [ \"$(cat \"$rel/$id/COMMIT\" 2>/dev/null)\" = \"$sha\" ] || die", "  true || die",
     [f'{TK}.Install.test_an_existing_release_must_be_the_real_one']),
    (REG, "=[A-Za-z0-9.:/,_-]*$' \"$dir/.env\"", "=.*$' \"$dir/.env\"", [f'{TK}.Register.test_a_hostile_env_line_stops_registration']),
    (REA, "            if os.path.islink(d) or not os.path.isdir(d):", "            if not os.path.isdir(d):",
     [f'{TE}.Reap.test_a_linked_owner_or_repository_is_not_walked']),
    (UNI, "[ \"$role\" = \"$1\" ] ||", "true ||", [f'{TK}.Uninstall.test_another_roles_inventory_is_refused']),
    (SET, "    if not isinstance(client, str) or not CLIENT.fullmatch(client):", "    if False:",
     [f'{TU}.Setup.test_the_proxy_client_is_one_absolute_path']),
    (UNI, "[ -z \"$left\" ] || {", "true || {", [f'{TK}.Uninstall.test_a_unit_still_active_removes_nothing']),
    (UNI, " and os.path.dirname(m['path']) == unit_dir", "", [f'{TK}.Uninstall.test_each_unit_is_disabled_and_only_setups_files_go']),
    (UNI, "  systemctl --user disable --now \"$u\" >/dev/null 2>&1 || true", "  systemctl --user disable --now \"$u\" >/dev/null 2>&1",
     [f'{TK}.Uninstall.test_each_unit_is_disabled_and_only_setups_files_go']),
    # place's probes and the claim step
    (PRB, "        if m and m.group(1).startswith(role + '-'):", "        if m:", [f'{TQ}.Probe.test_the_local_probe']),
    (PRB, "    ok = http_ok(h['proxy']['http_port'], timeout=20) if 'proxy' in h else True", "    ok = True",
     [f'{TQ}.Probe.test_the_local_probe']),
    (PRB, "reserved=d.get('reserved') is not False", "reserved=d.get('reserved') is True", [f'{TQ}.Probe.test_json_round_trip_and_fail_closed']),
    (PRB, "reachable=d.get('reachable') is True", "reachable=bool(d.get('reachable'))", [f'{TQ}.Probe.test_json_round_trip_and_fail_closed']),
    (PRB, "        return from_json(json.loads(r.stdout)) if r.returncode == 0 else place.Probe()",
     "        return from_json(json.loads(r.stdout))", [f'{TQ}.Probe.test_remote_probe_over_ssh']),
    (PRB, "        for f in concurrent.futures.as_completed(futs, timeout=bound):", "        for f in concurrent.futures.as_completed(futs):",
     [f'{TQ}.Probe.test_a_slow_probe_skips_its_host_within_the_bound']),
    (PRB, "and int(p[0]) in want", "", [f'{TQ}.Probe.test_the_local_probe']),
    (GHB, "    if c.run:\n        target", "    if True:\n        target", [f'{TQ}.Claim.test_skip_posts_nothing']),
    (GHB, "        if not c.run or now() >= deadline:", "        if True:", [f'{TQ}.Claim.test_the_wait_lets_a_local_claim_appear']),
    (GHB, "            if e.code < 500 and e.code != 429:\n                break", "            if False:\n                break",
     [f'{TQ}.Claim.test_api_retries_server_errors_only']),
    (GHB, "    if not SHA.fullmatch(sha) or", "    if", [f'{TQ}.Claim.test_bad_inputs']),
    (GHB, "    except (OSError, ValueError, ApiError) as e:", "    except (OSError, ValueError) as e:",
     [f'{TQ}.ClaimMain.test_an_api_error_exits_2_without_a_verdict']),
    (GHB, "f.write(f'run={\"true\" if c.run else \"false\"}", "f.write(f'run={\"true\"}",
     [f'{TQ}.ClaimMain.test_a_skip_writes_run_false']),
    (GHB, "e.code < 500 and e.code != 429", "e.code < 500", [f'{TQ}.Claim.test_api_retries_server_errors_only']),
    (GHB, "        if len(got) < PAGE:\n            break", "        break", [f'{TQ}.Claim.test_statuses_are_read_page_by_page']),
    (GHB, "        out += got\n", "        out = got\n", [f'{TQ}.Claim.test_statuses_are_read_page_by_page']),
    (GHB, "        if not isinstance(got, list):\n            raise ApiError", "        if False:\n            raise ApiError",
     [f'{TQ}.Claim.test_statuses_are_read_page_by_page']),
    (GHB, "        if a.hosted:\n            actors, bot", "        if False:\n            actors, bot",
     [f'{TQ}.ClaimMain.test_hosted_ignores_the_allowlist']),
    (PRB, "quarantined=d.get('quarantined') is not False", "quarantined=d.get('quarantined') is True",
     [f'{TQ}.Probe.test_json_round_trip_and_fail_closed']),
    (PRB, "c.get('healthy') is True", "bool(c.get('healthy', True))", [f'{TQ}.Probe.test_json_round_trip_and_fail_closed']),
    (PRB, "c.get('others') is not False", "bool(c.get('others', False))", [f'{TQ}.Probe.test_json_round_trip_and_fail_closed']),
    (PRB, "float(c['temp_c'])", "float(c.get('temp_c', 0))", [f'{TQ}.Probe.test_json_round_trip_and_fail_closed']),
    (PRB, "'ServerAliveCountMax=4', '--', ", "'ServerAliveCountMax=4', ", [f'{TQ}.Probe.test_remote_probe_over_ssh']),
    # the executor and the registry
    (PRT, "        if self.env.get('GITHUB_REPOSITORY', '').lower() != proj.repository:", "        if False:",
     [f'{TT}.Execute.test_refusals']),
    (PRT, "    env = {k: v for k, v in base.items() if not k.startswith('CI_') and k not in STEP_FILES}", "    env = {k: v for k, v in base.items() if k not in STEP_FILES}",
     [f'{TT}.Execute.test_a_passing_part_and_its_environment']),
    (PRT, "        'CI_RUNNERS_HOST': role, 'CI_PROJECT': project.name,", "        'CI_RUNNERS_HOST': role,",
     [f'{TT}.Execute.test_a_passing_part_and_its_environment']),
    (PRT, "            timed_out = True\n", "            timed_out = False\n", [f'{TT}.Execute.test_the_inner_timeout_ends_the_command']),
    (PRT, "        return (1 if out['outcome'] == 'red' else 0), out\n\n\n#", "        return 0, out\n\n\n#",
     [f'{TT}.Execute.test_red_is_the_only_failing_exit']),
    (PRT, "('full' if cls in SLOT_CLASSES else 'none')", "('none')", [f'{TT}.Execute.test_a_passing_part_and_its_environment']),
    (PRT, "            if free < h['disk_min_gib']:", "            if False:", [f'{TT}.Execute.test_low_disk_is_unavailable_before_the_command']),
    (PRT, "    if not real.startswith(root + os.sep):", "    if False:", [f'{TT}.Salvage.test_only_our_part_records']),
    (PRT, "    real = os.path.realpath(os.path.join(root, rel))", "    real = os.path.normpath(os.path.join(root, rel))", [f'{TT}.Salvage.test_only_our_part_records']),
    (PRT, "        retry_on = ['self-hosted', cls, role] if h.where == 'same-host' else", "        retry_on = ['self-hosted', cls] if h.where == 'same-host' else",
     [f'{TT}.Pure.test_reboot_stopped_and_lost']),
    (PRT, "+ (1 if unit_result == 'oom-kill' else 0)", "", [f'{TT}.Pure.test_oom_retries_once_with_resume']),
    (PRT, "            f.write('500')", "            f.write('0')", [f'{TT}.Pure.test_the_supervisor_records_the_exit']),
    (PRT, "            if registry.memory_bytes(memory) > registry.memory_bytes(proj.memory_max):", "            if False:",
     [f'{TT}.Execute.test_refusals']),
    (PRT, "                            if time.monotonic() >= deadline:\n                                return self._end(record, rec, a, unavailable='queue timeout: no CI slot')",
     "                            if time.monotonic() >= deadline + 3600:\n                                return self._end(record, rec, a, unavailable='queue timeout: no CI slot')",
     [f'{TT}.Execute.test_a_held_slot_makes_the_part_wait_then_unavailable']),
    (PRT, "        return int((prev or {}).get('attempt', 1)) + 1", "        return 1", [f'{TT}.Execute.test_a_retry_counts_its_attempt']),
    (PRT, "    child = subprocess.Popen(list(argv), start_new_session=True, preexec_fn=adj)",
     "    child = subprocess.Popen(list(argv), start_new_session=True, preexec_fn=adj, close_fds=False)",
     [f'{TT}.Execute.test_the_command_does_not_inherit_the_slot']),
    (PRT, "            leases.release()\n", "            pass\n", [f'{TT}.Execute.test_cards_are_leased']),
    (PRT, "        if self.proc is not None:\n            raise SystemExit(128 + signum)", "        if False:\n            raise SystemExit(128 + signum)",
     [f'{TT}.Execute.test_a_stopped_executor_leaves_the_part_to_salvage']),
    (PRT, "        os.setsid()\n", "        pass\n", [f'{TT}.Execute.test_a_stopped_executor_leaves_the_part_to_salvage']),
    (PRT, "env=env, pass_fds=held)", "env=env)", [f'{TT}.Execute.test_a_stopped_executor_leaves_the_part_to_salvage']),
    (PRT, "                    os.killpg(child.pid, signal.SIGKILL)", "                    pass", [f'{TT}.Pure.test_a_command_that_ignores_term_is_killed']),
    (PRT, "                        slot_fd = q.try_take(proj.name, caps, default=default)", "                        slot_fd = q.try_take(proj.name, caps, default=proj.name)",
     [f'{TT}.Execute.test_a_local_holder_counts_as_the_default_project']),
    (PRT, "                    caps.setdefault(proj.name, proj.max_slots)\n", "                    caps[proj.name] = 1\n",
     [f'{TT}.Execute.test_the_cap_holds_with_a_free_slot']),
    (PRT, "    if not record_ref(record).lower().startswith(", "    if False and not record_ref(record).lower().startswith(",
     [f'{TT}.Salvage.test_another_repository_cannot_salvage']),
    (PRT, "        if ex is not None:  # final", "        if True:  # final", [f'{TT}.Salvage.test_lost_is_not_kept_while_the_record_may_finish']),
    (PRT, "        stopped=stopped or bool(ex and ex.get('signalled') and not ex.get('timed_out')),", "        stopped=stopped,",
     [f'{TT}.Pure.test_reboot_stopped_and_lost']),
    (PRT, "        if a.gpu_mem_mib < 0 or a.queue_timeout_minutes < 0:", "        if False:", [f'{TT}.Execute.test_refusals']),
    (PRT, "            os.mkdir(record, 0o700)  # atomic", "            os.makedirs(record, 0o700, exist_ok=True)  # atomic",
     [f'{TT}.Execute.test_an_id_runs_once_per_run']),
    (PRT, " and k not in STEP_FILES}", "}", [f'{TT}.Execute.test_a_passing_part_and_its_environment']),
    (RGY, "(CI_|GITHUB_|ACTIONS_|RUNNER_|LD_|PYTHON).*|CUDA_VISIBLE_DEVICES|CUDA_DEVICE_ORDER|PATH|", "(CI_|GITHUB_|ACTIONS_|RUNNER_).*|CUDA_VISIBLE_DEVICES|",
     [f'{TT}.Registry.test_malformed']),
    (RGY, "    caps[default] = slots\n", "", [f'{TT}.Execute.test_an_older_local_ticket_keeps_its_turn']),
    (RGY, "            if default:\n                raise RegistryError", "            if False:\n                raise RegistryError",
     [f'{TT}.Execute.test_two_default_projects_are_refused']),
    (PRT, "part_dir=work, results=results", "part_dir=record, results=results", [f'{TT}.Execute.test_a_passing_part_and_its_environment']),
    (RGY, "        if not VAR.fullmatch(k) or RESERVED.fullmatch(k):", "        if not VAR.fullmatch(k):", [f'{TT}.Registry.test_malformed']),
    (RGY, "    if type(v) is not int or not lo <= v <= hi:", "    if not lo <= v <= hi:", [f'{TT}.Registry.test_malformed']),
    (RGY, " or (stem and name != stem)", "", [f'{TT}.Registry.test_malformed']),
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


def run(tree: pathlib.Path, tests: list[str], hang: float = HANG) -> int:
    # -B: a stale .pyc of an earlier mutant of the same size and second would be used
    try:  # no pipes: a holder a mutant leaves behind must not keep the run waiting
        return subprocess.run([sys.executable, '-B', '-m', 'unittest', '-q', *tests], cwd=tree,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=hang).returncode
    except subprocess.TimeoutExpired:
        return 124  # a hang fails the tests


def one(tree: pathlib.Path, rel: str, old: str, new: str, tests: list[str]) -> tuple[bool, float]:
    """Whether the tests kill one mutant, and their time, applied to ``tree`` (one worker's own copy)."""
    path = tree / rel
    src = path.read_text()
    path.write_text(src.replace(old, new))
    t0 = time.monotonic()
    try:
        return run(tree, tests) != 0, time.monotonic() - t0
    finally:
        path.write_text(src)


def main() -> int:
    for n, (rel, old, _new, _tests) in enumerate(MUTANTS, 1):
        count = (ROOT / rel).read_text().count(old)
        if count != 1:
            print(f'mutant {n}: the snippet occurs {count} times in {rel}', file=sys.stderr)
            return 2
    workers = max(1, min(WORKERS, os.cpu_count() or 1))
    with tempfile.TemporaryDirectory() as d:
        trees = queue.SimpleQueue()
        for w in range(workers):  # a tree per worker: a mutant is never seen by another's tests
            tree = pathlib.Path(d) / f'tree{w}'
            shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns('.git', '__pycache__'))
            trees.put(tree)
        every = sorted({t for *_, ts in MUTANTS for t in ts})
        tree0 = trees.get()
        if run(tree0, every, hang=600):
            print('the unmutated tree fails its tests', file=sys.stderr)
            return 2
        trees.put(tree0)

        def job(m):
            tree = trees.get()
            try:
                return one(tree, *m)
            finally:
                trees.put(tree)
        with concurrent.futures.ThreadPoolExecutor(workers) as ex:
            killed = list(ex.map(job, MUTANTS))
    for n, ((rel, old, *_), (k, secs)) in enumerate(zip(MUTANTS, killed), 1):
        print(f'{"killed  " if k else "SURVIVED"} {n:3d} {secs:5.1f}s {rel}: {old.strip()[:70]!r}')
    survived = [k for k, _ in killed].count(False)
    print(f'{len(MUTANTS) - survived}/{len(MUTANTS)} mutants killed ({workers} workers)')
    return 1 if survived else 0


if __name__ == '__main__':
    sys.exit(main())
