"""Focused source, timing and report-gate faults, each in a fresh interpreter."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
CASES = []


def case(label, module, before, after, suite, test):
    CASES.append((label, module, before, after, 'parallel.tests.'+suite+'.'+test))


S = 'test_source_selection'
C = 'SourceSelection.'
case('source-head-provenance', 'parallel.source_selection', 'run_head_sha=head, actors=permissions',
     'run_head_sha=sha, actors=permissions', S, C+'test_authorized_pr_dispatch_keeps_both_source_and_test_sha')
case('strict-default-sha', 'parallel.github_coordinator', "if run.get('head_sha') != requested['sha']:", 'if False:',
     S, C+'test_run_head_default_is_not_weakened_or_actor_expanded')
case('repository-opt-in', 'parallel.source_selection',
     "if (allowed.get('repository') != known.name.lower() or choice['kind'] not in allowed.get('kinds', ())):", 'if False:',
     S, C+'test_override_is_opt_in_to_exact_repository_id_and_name')
case('both-human-actors', 'parallel.source_selection', "or access.get('permission') not in ('write', 'admin')", 'or False',
     S, C+'test_original_and_triggering_actor_each_require_human_write')
case('same-repository-head', 'parallel.source_selection', "or pr.get('head', {}).get('sha') != sha", 'or False',
     S, C+'test_fork_wrong_or_moved_pr_head_and_source_event_are_refused')
case('ancestor-base', 'parallel.source_selection', "or compared.get('merge_base_commit', {}).get('sha') != base", 'or False',
     S, C+'test_replay_resolves_exact_commit_and_ancestor_without_retargeting')
case('typed-pr-number', 'parallel.source_selection', "not protocol.positive(value['number'])", "not isinstance(value['number'], int)",
     S, C+'test_selector_types_and_range_cannot_smuggle_overrides')
case('private-policy-schema-type', 'parallel.source_selection', "type(doc['schema']) is not int", 'False',
     S, C+'test_private_policy_types_duplicates_and_exact_binding')

S = 'test_timing'
C = 'Timing.'
case('no-generic-timing', 'parallel.protocol', "p.get('gpu') or device in ('', 'slurm')", "p.get('gpu') or device == ''",
     S, C+'test_timing_plan_and_grant_cannot_be_generic_gpu_or_old_schema')
case('complete-group-reservation', 'parallel.scheduler_store', "groups_required = part['cores'] // protocol.GROUP_CPUS",
     "groups_required = 1", S, C+'test_timing_blocks_cpu_and_gpu_until_full_release')
case('old-executor-refused', 'parallel.scheduler_store', "5 if part.get('isolation') == 'timing' else 4 if remote_gpu else 3",
     "4 if remote_gpu else 3", S, C+'test_timing_waits_for_existing_cpu_and_older_executor_or_changed_profile')
case('claim-width-refence', 'parallel.scheduler_store', "if pool is None or pool['total'] != a['groups']:", 'if False:',
     S, C+'test_resized_device_cannot_claim_an_old_full_device_grant')
case('full-profile-width', 'parallel.timing', "or p['device'] != task.get('device') or p['cores'] != task['cores']",
     "or p['device'] != task.get('device')", S, C+'test_private_profile_disallows_partial_width_and_foreign_repository')
case('reference-affinity-applied', 'parallel.timing', "os.sched_setaffinity(0, p['cpus'])", 'pass',
     S, C+'test_real_lock_is_inherited_and_restored_on_failure')
case('nice-zero-required', 'parallel.timing', "if os.getpriority(os.PRIO_PROCESS, 0) != 0:", 'if False:',
     S, C+'test_wrong_priority_affinity_model_or_profile_never_gets_lock_cover')
case('timing-command-priority', 'parallel.task_execution', 'nice=0 if timing_receipt else 10', 'nice=10',
     S, C+'test_supervisor_preserves_nice_zero_and_publishes_isolation_receipt')
case('real-exclusive-lock', 'parallel.timing', "holder.acquire('x')", "holder.acquire('s')",
     S, C+'test_real_lock_is_inherited_and_restored_on_failure')
case('termination-lock-required', 'parallel.timing', "holder.acquire('x')", "raise protocol.Error('no lock')",
     S, C+'test_process_termination_releases_exclusive_hold')

S = 'test_reporting'
C = 'Gate.'
case('no-early-primary-success', 'parallel.scheduler_store', "publication = report['state'] if report['state'] in ('success', 'error') else 'pending'",
     "publication = 'success'", S, C+'test_success_waits_for_both_receipt_verification_and_job_completion')
case('failed-reader-red', 'parallel.scheduler_store', "('success' if success else 'error', reason[:256], batch_id)",
     "('success', reason[:256], batch_id)", S, C+'test_missing_or_failed_reader_never_publishes_success')
case('controller-rechecks-projection', 'parallel.github_coordinator', "reporting.verify_receipt(self.storage_etc, batch, report['reference'])",
     'pass', S, C+'test_successful_job_without_receipt_and_corrupt_receipt_fail_closed')
case('reporter-grace-bounded', 'parallel.github_coordinator', "self.clock()-report['source_ended'] >= 60", 'False',
     S, C+'test_missing_reporter_has_bounded_source_completion_grace')
case('immutable-job-submission', 'parallel.scheduler_store', "if row['reference'] != value or row['job_id'] != job_id:", 'if False:',
     S, C+'test_immutable_submission_survives_restart_and_rejects_conflicts')
case('cancel-success-withdrawn', 'parallel.scheduler_store', "WHERE batch=? AND state!='error'", "WHERE batch=? AND state='never'",
     S, C+'test_cancellation_after_reader_success_invalidates_publication')
case('completed-history-preserved', 'parallel.scheduler_store', "AND (state!='success' OR ?)", 'AND (? OR 1)',
     S, C+'test_supersession_closes_pending_gate_and_preserves_completed_history')
case('native-red-kept', 'parallel.scheduler_store', "if report is not None and b['state'] == 'success':", 'if report is not None:',
     S, C+'test_native_failure_stays_red_when_report_also_fails')
case('ungated-not-enrolled', 'parallel.scheduler_store', "if source.get('report_gate') is True:", 'if True:',
     S, C+'test_ungated_success_remains_compatible')
case('authorized-report-job', 'parallel.scheduler', "or job.get('runner_name') != params.get('runner')", 'or False',
     S, C+'test_report_rpc_checks_source_attempt_job_runner_and_immutable_identity')
C = 'Receipt.'
case('real-manifest-success', 'parallel.reporting', "return reference\n", "raise storage.Error('corrupt')\n",
     S, C+'test_complete_success_compares_real_native_and_projected_manifests')
case('required-junit', 'parallel.reporting', "require_members(native, part.get('required_files', []))", 'pass',
     S, C+'test_success_without_junit_is_rejected')
case('nonempty-rows', 'parallel.reporting', "if final and name != 'impact' and (ref is None or not subtree or", 'if False and (',
     S, C+'test_success_without_nonempty_rows_is_rejected')
case('outcome-projection-exact', 'parallel.reporting', "if copied != {name: native[name] for name in ('outcome.json', 'summary.md')}:",
     'if False:', S, C+'test_changed_projection_is_rejected')
case('producer-sha-metadata', 'parallel.reporting', "if members.pop('.ci-storage-source.json', None) != encoded(expected):",
     "if members.pop('.ci-storage-source.json', None) is None:", S, C+'test_wrong_checkout_provenance_is_rejected')
case('complete-attempt-set', 'parallel.reporting', "or set(doc['allocations']) != {a['id'] for a in batch['allocations']}", 'or False',
     S, C+'test_omitted_attempt_and_false_selection_are_rejected')
case('source-attempt-binding', 'parallel.reporting', "or not same(doc['source'], {key: source[key] for key in SOURCE_FIELDS})", 'or False',
     S, C+'test_foreign_source_attempt_is_rejected')
case('selection-verified', 'parallel.reporting', "or doc['selection_verified'] is not True", 'or False',
     S, C+'test_false_selection_flag_is_rejected')
case('required-projection', 'parallel.reporting', "if final and name != 'impact' and (ref is None or not subtree or", 'if False and (',
     S, C+'test_required_projection_and_seal_cannot_be_omitted')
case('accepted-native-reference', 'parallel.reporting', "or not same(p['native_reference'], a['storage_reference'])", 'or False',
     S, C+'test_controller_native_reference_cannot_be_replaced')
case('verified-red-allowed', 'parallel.reporting', "if (red or a['result'].get('state') == 'failure') and batch['state'] == 'success':",
     "if red or a['result'].get('state') == 'failure':", S, C+'test_genuine_red_receipt_can_verify_without_turning_execution_green')
C = 'ReportPolicy.'
case('schema-four-drain', 'parallel.scheduler_store', "if pending or active:", 'if False:',
     S, C+'test_schema_four_upgrade_requires_drain_and_preserves_ungated_history')
case('final-client-publication', 'parallel.workflow_client', "if publication and 'publication_state' in record:", 'if False:',
     S, C+'test_final_client_waits_for_publication_but_execution_reader_does_not')
case('no-gate-downgrade', 'parallel.reporting', 'if matching and not requested:', 'if False:',
     S, C+'test_required_context_cannot_disable_or_spoof_namespace')
case('typed-report-minimum', 'parallel.reporting', "not protocol.positive(rule['min_bytes'])", "not isinstance(rule['min_bytes'], int)",
     S, C+'test_required_file_paths_are_bounded_relative_patterns')
case('read-only-report-job', '.github/workflows/ci-v2.yml', '      pull-requests: read\n    steps:\n      - uses: actions/checkout@v4',
     '      pull-requests: write\n    steps:\n      - uses: actions/checkout@v4', S,
     C+'test_report_job_has_no_write_authority_and_verdict_has_no_cycle')
case('policy-plan-fingerprint', 'parallel.deployment_config', 'if fingerprints:', 'if False:',
     'test_organization_deployment', 'OrganizationDeployment.test_optional_feature_policies_are_role_scoped_private_and_plan_bound')
case('inventory-owned-reference-lock', 'parallel.deployment_config', "hosts[name]['files']['host_lock'] = profiles[0]['host_lock']", 'pass',
     'test_organization_deployment', 'OrganizationDeployment.test_timing_profile_renders_one_inventory_lock_for_complete_local_budget')
case('controller-only-source-policy', 'parallel.deployment_config', '(self.is_controller or not controller_only)', 'True',
     'test_organization_deployment', 'OrganizationDeployment.test_optional_feature_policies_are_role_scoped_private_and_plan_bound')
case('timing-budget-binding', 'parallel.deployment_config', "any(p['cpu_count'] != cpu_count(specification) for p in profiles)", 'False',
     'test_organization_deployment', 'OrganizationDeployment.test_timing_profile_renders_one_inventory_lock_for_complete_local_budget')
case('policy-content-changes-fingerprint', 'parallel.deployment_config', 'hashlib.sha256(deployment_files.read_private(path)).hexdigest()',
     "'0'*64", 'test_organization_deployment', 'OrganizationDeployment.test_optional_feature_policies_are_role_scoped_private_and_plan_bound')


case('gated-cpu-old-executor', 'parallel.scheduler_store',
     "source.get('report_gate') is True or source.get('source_selection') is not None",
     "source.get('source_selection') is not None", 'test_memory_requests',
     'MemoryRequests.test_report_gated_cpu_waits_for_matching_executor_codec')
case('override-cpu-old-executor', 'parallel.scheduler_store',
     "source.get('report_gate') is True or source.get('source_selection') is not None",
     "source.get('report_gate') is True", 'test_memory_requests',
     'MemoryRequests.test_overridden_cpu_source_waits_for_matching_executor_codec')
case('new-contract-codec-floor', 'parallel.scheduler_store', 'modern is None or modern[0] < 5',
     'modern is None or modern[0] < 4', 'test_memory_requests',
     'MemoryRequests.test_report_gated_cpu_waits_for_matching_executor_codec')

case('new-contract-missing-codec', 'parallel.scheduler_store', 'modern is None or modern[0] < 5',
     'modern is not None and modern[0] < 5', 'test_memory_requests',
     'MemoryRequests.test_new_contract_waits_without_executor_capability_advertisement')


def main():
    code = '''import importlib,pathlib,sys,unittest
from unittest import mock
name,before,after,case=sys.argv[1:]
if name.endswith('.yml'):
 path=pathlib.Path(name).resolve(); source=path.read_text(); original=pathlib.Path.read_text
 if source.count(before)!=1:raise SystemExit(3)
 replacement=source.replace(before,after,1)
 def read(p,*a,**kw):return replacement if p.resolve()==path else original(p,*a,**kw)
 patch=mock.patch.object(pathlib.Path,'read_text',read);patch.start()
else:
 module=importlib.import_module(name);path=pathlib.Path(module.__file__);source=path.read_text()
 if source.count(before)!=1:raise SystemExit(3)
 exec(compile(source.replace(before,after,1),str(path),'exec'),module.__dict__)
result=unittest.TextTestRunner(verbosity=0).run(unittest.defaultTestLoader.loadTestsFromName(case))
raise SystemExit(0 if result.wasSuccessful() else 1)
'''
    for label, module, before, after, test in CASES:
        if label == 'termination-lock-required':
            # The guard runs in a child interpreter; use a private source copy so
            # that interpreter also sees the mutation. Never edit the checkout.
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ('parallel', 'ci_runners', 'tests'):
                    shutil.copytree(ROOT/name, root/name, ignore=shutil.ignore_patterns('__pycache__'))
                path = root/'parallel/timing.py'
                source = path.read_text()
                if source.count(before) != 1:
                    raise SystemExit(f'{label}: mutation target is ambiguous')
                path.write_text(source.replace(before, after, 1))
                result = subprocess.run([sys.executable, '-m', 'unittest', test, '-q'], cwd=root,
                                        capture_output=True, text=True, timeout=30)
        else:
            result = subprocess.run([sys.executable, '-c', code, module, before, after, test], cwd=ROOT,
                                    capture_output=True, text=True, timeout=30)
        if result.returncode != 1 or 'SyntaxError' in result.stderr or 'Failed to import test module' in result.stderr:
            print(result.stderr[-3000:])
            raise SystemExit(f'{label}: survived or could not run (exit {result.returncode})')
        print(label+': killed by '+test, flush=True)
    print(f'{len(CASES)} mutants killed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
