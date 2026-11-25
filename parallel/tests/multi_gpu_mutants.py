"""Run focused two-card safety mutants in isolated Python interpreters."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
MUTANTS = (
    ('no-central-exclusion', 'parallel.scheduler_store',
     'WHERE ac.uuid=c.uuid AND a.released=0)', 'WHERE ac.uuid=c.uuid AND a.released=1)',
     'Central.test_concurrent_pairs_have_disjoint_cards_and_preserve_memory_and_cpus'),
    ('old-executor-accepted', 'parallel.scheduler_store',
     "if capability is None or capability[0] < (5 if part.get('isolation') == 'timing' else 4 if remote_gpu else 3):", 'if False:',
     'Central.test_pair_does_not_span_devices_or_reach_old_executors'),
    ('duplicate-grant-cards', 'parallel.protocol',
     'validate_gpu_list(device, cards)\n    elif', 'pass\n    elif',
     'Contracts.test_pair_cannot_be_truncated_duplicated_or_disguised_as_old_schema'),
    ('reserve-first-card-only', 'parallel.executor_store',
     'for card in protocol.grant_cards(grant):', 'for card in protocol.grant_cards(grant)[:1]:',
     'Local.test_partial_local_failure_rolls_back_cards_cpus_and_execution'),
    ('fence-first-card-only', 'parallel.task_execution',
     'for card in protocol.grant_cards(grant):', 'for card in protocol.grant_cards(grant)[:1]:',
     'Local.test_scope_fence_requires_every_card_and_cleanup_releases_the_pair'),
    ('no-p2p-gate', 'parallel.task_execution',
     'if len(cards) == 2:', 'if False:',
     'Local.test_p2p_gate_precedes_command_and_visibility_contains_the_complete_pair'),
    ('release-unrelated-cards', 'parallel.executor_store',
     "DELETE FROM held_gpus WHERE allocation=?', (aid,)", "DELETE FROM held_gpus WHERE ? IS NOT NULL', (aid,)",
     'Local.test_scope_fence_requires_every_card_and_cleanup_releases_the_pair'),
    ('rotate-first-card-only', 'parallel.scheduler_store',
     "for card in protocol.grant_cards(a['grant']):", "for card in protocol.grant_cards(a['grant'])[:1]:",
     'Central.test_released_pair_rotates_both_cards_without_duplicate_capacity_rows'),
)


def main():
    from parallel.tests.gpu_slurm_mutants import MUTANTS as SLURM_MUTANTS
    cases = [(*entry[:-1], 'parallel.tests.test_multi_gpu.' + entry[-1]) for entry in MUTANTS]
    cases += [(*entry[:-1], 'parallel.tests.test_gpu_slurm.' + entry[-1]) for entry in SLURM_MUTANTS]
    for label, module, before, after, case in cases:
        code = '''import importlib,pathlib,sys,unittest
module=importlib.import_module(sys.argv[1]);path=pathlib.Path(module.__file__)
source=path.read_text();before,after=sys.argv[2:4]
if source.count(before)!=1:raise SystemExit(3)
exec(compile(source.replace(before,after,1),str(path),'exec'),module.__dict__)
suite=unittest.defaultTestLoader.loadTestsFromName(sys.argv[4])
result=unittest.TextTestRunner(verbosity=0).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
'''
        result = subprocess.run([sys.executable, '-c', code, module, before, after, case],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        if result.returncode != 1:
            raise SystemExit(f'{label}: survived or could not run (exit {result.returncode})')
        print(label + ': killed by ' + case)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
