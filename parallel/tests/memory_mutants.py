"""Five focused faults in the optional RAM contract must be detected by its tests."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CASES = [
    ('parallel/protocol.py', "'memory_gib' in p and (not positive(p['memory_gib']) or p['memory_gib'] > (1 << 20))", 'False',
     'test_invalid_explicit_limits_fail_closed'),
    ('parallel/scheduler_store.py', 'occupied_memory + requested_memory > memory_limit', 'False',
     'test_transaction_prevents_concurrent_ram_overcommit'),
    ('parallel/scheduler_store.py', "WHERE a.host=? AND a.pool!='slurm' AND a.released=0", "WHERE a.host=? AND a.pool!='slurm' AND a.released=1",
     'test_gpu_and_cpu_share_the_same_host_ram_budget'),
    ('parallel/scheduler_store.py', 'grant.update(memory_gib=p[\'memory_gib\'], RAM=f\'{p["memory_gib"]}G\')',
     'grant.update(memory_gib=p[\'memory_gib\'], RAM=f\'{p["cores"]}G\')', 'test_explicit_ram_preserves_cores_and_workers'),
    ('parallel/protocol.py', 'not positive(memory) or memory > (1 << 20) or value.get(\'RAM\') != f\'{memory}G\'', 'False',
     'test_explicit_ram_preserves_cores_and_workers'),
]


def main():
    for number, (name, old, new, test) in enumerate(CASES, 1):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for package in ('ci_runners', 'parallel'):
                shutil.copytree(ROOT / package, root / package, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            target = root / name
            source = target.read_text(encoding='utf-8')
            if source.count(old) != 1:
                raise RuntimeError(f'mutant {number}: source anchor changed')
            target.write_text(source.replace(old, new), encoding='utf-8')
            result = subprocess.run([sys.executable, '-m', 'unittest',
                                     'parallel.tests.test_memory_requests.MemoryRequests.' + test],
                                    cwd=root, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(root)},
                                    capture_output=True, text=True, timeout=30)
            if not result.returncode or ('FAILED (' not in result.stderr):
                raise RuntimeError(f'mutant {number} survived or failed to run: {result.stderr[-1000:]}')
            print(f'memory mutant {number}: detected by {test}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
