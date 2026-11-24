"""Fault injections for internal storage; each new test kills a guarded mutation."""
T = 'parallel.tests.test_storage.'
F = 'ci_runners/storage.py'
H = 'ci_runners/storage_host.py'
S = 'ci_runners/storage_ssh.py'
C = 'parallel/github_coordinator.py'
CASES = [
    (S, '                            os.killpg(process.pid, signal.SIGKILL)', '                            pass',
     [T+'Transport.test_timeout_kills_the_transport_session_and_waits_for_it']),
    (F, '    if deadline is not None and time.monotonic() >= deadline:', '    if False:',
     [T+'BundleFiles.test_packing_deadline_is_inside_the_operation_budget']),
    ('ci_runners/cli.py', "            if selected['backend'] == 'ssh':", '            if False:',
     [T+'BundleFiles.test_legacy_opt_in_refuses_before_execution']),
    ('parallel/scheduler.py', "            bundles.verify_plan(self.etc, source, source.get('files_bundle'))", '            pass',
     [T+'ProductionBridge.test_plan_publication_fetch_and_submission_share_one_reference']),
    (H, '            os.rename(path, final)', '            pass',
     [T+'BundleFiles.test_roundtrip_idempotency_and_staging_visibility']),
    (H, "if any(old['manifest'][key] != doc[key] for key in ('identity', 'size', 'sha256', 'members', 'retention')):", 'if False:',
     [T+'BundleFiles.test_conflicting_content_never_overwrites']),
    (F, "or cfg.get('default_backend', 'github') != 'github'", 'or False',
     [T+'BundleFiles.test_missing_config_defaults_and_invalid_config_refuses']),
    (F, "or file_digest(payload, deadline) != doc['sha256']", 'or False',
     [T+'BundleFiles.test_digest_and_identity_mismatch_are_rejected']),
    (F, "if expected is not None and ident != identity(expected) or store is not None and value['store'] != store:", 'if False:',
     [T+'BundleFiles.test_manifest_reference_cannot_select_another_attempt']),
    (F, 'if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):', 'if False:',
     [T+'BundleFiles.test_limits_and_symlink_inputs_are_rejected']),
    (F, "        raise Error('corrupt')\n    return name", '        return name\n    return name',
     [T+'BundleFiles.test_zip_traversal_duplicates_and_links_are_rejected']),
    (H, '        if dry_run is not True:', '        if False:',
     [T+'BundleFiles.test_cleanup_is_protected_and_destructive_mode_refuses']),
    (F, '    if destination.exists() or destination.is_symlink():', '    if False:',
     [T+'BundleFiles.test_existing_destination_is_not_merged']),
    (S, 'StrictHostKeyChecking=yes', 'StrictHostKeyChecking=no',
     [T+'Transport.test_ssh_is_noninteractive_pinned_and_does_not_expose_stderr']),
    (S, "range(self.limits['attempts'])", 'range(1)',
     [T+'Transport.test_retry_budget_is_shared_and_permanent_errors_do_not_retry']),
    ('parallel/scheduler_store.py', '            if candidate is not None:\n                candidate = storage.binding(candidate)',
     '            if candidate is not None:\n                db.execute(\'DELETE FROM source_storage WHERE id=?\', (ident,))\n                candidate = storage.binding(candidate)',
     [T+'CentralStorage.test_binding_survives_configuration_changes_and_restart']),
    (C, "                if not allocation['released']:\n                    continue", '                if False:\n                    continue',
     [T+'CentralStorage.test_coordinator_requires_cleanup_job_and_verified_bundle']),
    (C, 'storage_reference=receipt)', 'storage_reference=None)',
     [T+'CentralStorage.test_verified_result_can_finish_without_github_artifacts']),
    ('parallel/scheduler_store.py', "        result = dict(a['result'])", "        result = {**a['result'], 'state': 'success'}",
     [T+'CentralStorage.test_failed_test_stays_failed_when_publication_also_fails']),
    ('parallel/bundles.py', "or doc.get('result') != allocation['result']", 'or False',
     [T+'ProductionBridge.test_result_content_is_checked_against_executor_evidence']),
    ('.github/workflows/execute.yml', " && steps.allocation.outputs.storage_backend == 'ssh'", '',
     [T+'ProductionBridge.test_workflow_routes_each_publication_to_exactly_one_backend']),
]


def main():
    import pathlib
    import shutil
    import tempfile
    from tests import mutants
    root = pathlib.Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='storage-mutants-') as temporary:
        tree = pathlib.Path(temporary)/'tree'
        shutil.copytree(root, tree, ignore=shutil.ignore_patterns('.git', '__pycache__', '.venv'))
        mutants.stamp.stamp(tree, 'hook')
        for path, old, _, _ in CASES:
            if (tree/path).read_text().count(old) != 1:
                raise SystemExit(f'mutation snippet does not match: {path}')
        tests = sorted({test for _, _, _, cases in CASES for test in cases})
        if mutants.run(tree, tests):
            raise SystemExit('unmutated storage tests failed')
        results = [mutants.one(tree, *case)[0] for case in CASES]
    for case, killed in zip(CASES, results):
        print(f'{"killed" if killed else "SURVIVED"}: {case[0]}: {case[3][0]}')
    print(f'{sum(results)}/{len(results)} storage mutants killed')
    return 0 if all(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
