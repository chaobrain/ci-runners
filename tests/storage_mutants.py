"""Fault injections for the v1 storage protocol and paired readers."""
from __future__ import annotations
import pathlib
import shutil
import tempfile
from tests import mutants

CASES = [('ci_runners/storage_ssh.py', "elif b'permission denied' in private_error:", 'elif False:', ['tests.test_storage_bundle.Transport.test_ssh_is_noninteractive_pinned_and_does_not_expose_stderr']), ('ci_runners/storage.py', "archive.open(info, 'w', force_zip64=True)", "archive.open(info, 'w')", ['tests.test_storage_bundle.BundleFiles.test_zip64_publication_and_local_failures_keep_the_contract']), ('ci_runners/storage.py', '        except OSError as exc:\n            code = {errno.EACCES:', '        except BrokenPipeError as exc:\n            code = {errno.EACCES:', ['tests.test_storage_bundle.BundleFiles.test_zip64_publication_and_local_failures_keep_the_contract']), ('ci_runners/storage_ssh.py', '                            os.killpg(process.pid, signal.SIGKILL)', '                            pass', ['tests.test_storage_bundle.Transport.test_timeout_kills_the_transport_session_and_waits_for_it']), ('ci_runners/storage.py', '    if deadline is not None and time.monotonic() >= deadline:', '    if False:', ['tests.test_storage_bundle.BundleFiles.test_packing_deadline_is_inside_the_operation_budget']), ('ci_runners/storage_host.py', '            os.rename(path, final)', '            pass', ['tests.test_storage_bundle.BundleFiles.test_roundtrip_idempotency_and_staging_visibility']), ('ci_runners/storage_host.py', "if any(old['manifest'][key] != doc[key] for key in ('identity', 'size', 'sha256', 'members', 'retention')):", 'if False:', ['tests.test_storage_bundle.BundleFiles.test_conflicting_content_never_overwrites']), ('ci_runners/storage.py', "or cfg.get('default_backend', 'github') != 'github'", 'or False', ['tests.test_storage_bundle.BundleFiles.test_missing_config_defaults_and_invalid_config_refuses']), ('ci_runners/storage.py', "or file_digest(payload, deadline) != doc['sha256']", 'or False', ['tests.test_storage_bundle.BundleFiles.test_digest_and_identity_mismatch_are_rejected']), ('ci_runners/storage.py', "if expected is not None and ident != identity(expected) or store is not None and value['store'] != store:", 'if False:', ['tests.test_storage_bundle.BundleFiles.test_manifest_reference_cannot_select_another_attempt']), ('ci_runners/storage.py', 'if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):', 'if False:', ['tests.test_storage_bundle.BundleFiles.test_limits_and_symlink_inputs_are_rejected']), ('ci_runners/storage.py', "        raise Error('corrupt')\n    return name", '        return name\n    return name', ['tests.test_storage_bundle.BundleFiles.test_zip_traversal_duplicates_and_links_are_rejected']), ('ci_runners/storage_host.py', '        if dry_run is not True:', '        if False:', ['tests.test_storage_bundle.BundleFiles.test_cleanup_is_protected_and_destructive_mode_refuses']), ('ci_runners/storage.py', '    if destination.exists() or destination.is_symlink():', '    if False:', ['tests.test_storage_bundle.BundleFiles.test_existing_destination_is_not_merged']), ('ci_runners/storage_ssh.py', 'StrictHostKeyChecking=yes', 'StrictHostKeyChecking=no', ['tests.test_storage_bundle.Transport.test_ssh_is_noninteractive_pinned_and_does_not_expose_stderr']), ('ci_runners/storage_ssh.py', "range(self.limits['attempts'])", 'range(1)', ['tests.test_storage_bundle.Transport.test_retry_budget_is_shared_and_permanent_errors_do_not_retry'])]

V='ci_runners/storage_v1.py'
T='tests.test_storage_v1.'
CASES += [
 (V, "if not matching or any(job.get('conclusion') != 'skipped' for job in matching):", "if False:",
  [T+'Bundles.test_rerun_keeps_a_previous_pass_only_for_confirmed_skipped_jobs',
   'tests.test_storage_pipeline.Pipeline.test_failed_rerun_upload_cannot_reveal_a_previous_success']),
 ('ci_runners/part.py', "env['CI_STORAGE_STORE'] = base['CI_STORAGE_STORE']", "pass",
  ['tests.test_storage_pipeline.Pipeline.test_part_preserves_only_the_logical_storage_requirement']),
 (V, '                    found.pop(missing, None)', '                    pass',
  [T+'Bundles.test_optional_absence_shadows_earlier_reports_but_skipped_jobs_keep_theirs']),
 (V, "not hidden and any(p.startswith('.') for p in rel.parts)", "any(p.startswith('.') for p in rel.parts)",
  [T+'Bundles.test_roundtrip_includes_hidden_files_when_requested']),
 (V, "if dest in staged and storage.file_digest(staged[dest]) != storage.file_digest(path):", "if False:",
  [T+'Bundles.test_pattern_collision_and_corruption_leave_no_partial_download']),
 (V, "        return out\n    except (KeyError, TypeError)", "        out['head_sha'] = 'a' * 40\n        return out\n    except (KeyError, TypeError)",
  [T+'Bundles.test_workflow_sha_and_backend_binding_cannot_be_substituted']),
 (V, "deadline = time.monotonic() + self.limits['operation_seconds']", "deadline = None",
  [T+'Bundles.test_enumeration_obeys_the_shared_operation_deadline']),
 (V, "        if run['id'] < self.cutover:\n            return None", "        if run['id'] <= self.cutover:\n            return None",
  [T+'Bundles.test_history_adapter_preserves_pre_cutover_reads_and_verifies_new_bytes']),
 (V, "if not current_binding and not (allow_empty and total == 0):", "if False:",
  [T+'Bundles.test_new_runs_never_fall_back_when_binding_or_bundle_is_missing',
   T+'Bundles.test_completed_history_without_any_publication_is_an_empty_listing']),
 (V, "            return 'required project report receipt missing'", "            return ''",
  [T+'Bundles.test_seal_is_required_for_a_successful_outcome']),
 (V, "        if missing or state != 'success':", "        if False:",
  [T+'Bundles.test_failed_or_incomplete_report_publication_cannot_be_green']),
 (V, "if any(p.is_symlink() for p in (path, *path.parents)):", "if False:",
  [T+'Bundles.test_links_in_producers_and_destination_parents_are_rejected']),
 (V, "    if cutover is None:\n        if required:", "    if cutover is None:\n        if False:",
  [T+'Configuration.test_a_required_internal_store_never_silently_selects_github']),
 (V, "info.get('private') is not True", "False",
  [T+'Configuration.test_current_source_rejects_public_fork_and_wrong_repository_id']),
 (V, "        if not wanted:\n            raise storage.Error('missing')", "        if not wanted:\n            return 0",
  [T+'Bundles.test_empty_optional_upload_and_required_download_have_distinct_results']),
 ('ci_runners/storage.py', "type(run) is not int or run <= 0", "not isinstance(run, int) or run < 0",
  [T+'Configuration.test_cutover_requires_a_positive_integer_and_a_matching_ssh_override']),
 ('ci_runners/verdict.py', "if internal is not None and v.state == 'success':", "if False:",
  ['tests.test_storage_pipeline.Pipeline.test_no_success_without_both_outcome_and_report_receipt']),
 ('ci_runners/verdict.py', "Verdict('error', 'internal files: ' + e.code)", "Verdict('success', 'internal files: ' + e.code)",
  ['tests.test_storage_pipeline.Pipeline.test_corrupt_transport_posts_error_instead_of_reading_github']),
]


def main():
    root=pathlib.Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='storage-mutants-') as directory:
        tree=pathlib.Path(directory)/'tree'
        shutil.copytree(root,tree,ignore=shutil.ignore_patterns('.git','__pycache__'))
        mutants.stamp.stamp(tree,'hook')
        for path,old,_,_ in CASES:
            if (tree/path).read_text().count(old)!=1:
                raise SystemExit('mutation snippet does not match: '+path+' '+old)
        tests=sorted({test for *_,ts in CASES for test in ts})
        if mutants.run(tree,tests):raise SystemExit('unmutated storage tests failed')
        results=[mutants.one(tree,*case)[0] for case in CASES]
    for case,killed in zip(CASES,results):
        print(('killed' if killed else 'SURVIVED')+': '+case[0]+': '+','.join(case[3]))
    print(str(sum(results))+'/'+str(len(results))+' storage mutants killed')
    return 0 if all(results) else 1


if __name__=='__main__':raise SystemExit(main())
