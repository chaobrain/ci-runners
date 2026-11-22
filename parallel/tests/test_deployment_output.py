"""The operator view is a copy, including committed SQLite WAL pages."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from parallel import deployment_output


class DeploymentOutput(unittest.TestCase):
    def test_refresh_reads_committed_live_database_and_only_copies_listed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            etc = runtime / 'ci-runners/etc'
            control = runtime / 'parallel'
            state = control / 'state'
            state.mkdir(parents=True)
            (etc / 'projects').mkdir(parents=True)
            (etc / 'inventory.toml').write_text('schema = 2\n')
            (etc / 'policy.toml').write_text('schema = 2\n')
            (etc / 'projects/demo.toml').write_text('name = "demo"\n')
            (state / 'deployment.json').write_text('{"status": "installed"}\n')
            (state / 'scheduler-health.json').write_text('{"connected": true}\n')
            (control / 'etc/private').mkdir(parents=True)
            (control / 'etc/private/github-pat').write_text('fixture\n')
            output = runtime / 'output'
            output.mkdir()
            (output / 'devices.json').write_text('{"devices":{}}\n')
            config = SimpleNamespace(runtime=runtime, organization_mode=False, etc=etc, control=control,
                                     is_controller=True, host={'roles': ['controller']})

            with closing(sqlite3.connect(state / 'scheduler.sqlite')) as live:
                live.execute('PRAGMA journal_mode=WAL')
                live.execute('CREATE TABLE hosts (name TEXT)')
                live.execute("INSERT INTO hosts VALUES ('first')")
                live.commit()
                self.assertTrue((state / 'scheduler.sqlite-wal').is_file())
                result = deployment_output.refresh(config)
                self.assertEqual(result['files'], sorted([
                    'inventory.toml', 'policy.toml', 'projects/demo.toml', 'deployment.json',
                    'devices.json', 'scheduler.sqlite', 'scheduler-health.json',
                ]))
                self.assertFalse(result['missing'])
                self.assertFalse((output / 'github-pat').exists())
                with closing(sqlite3.connect(output / 'scheduler.sqlite')) as copy:
                    self.assertEqual(copy.execute('SELECT * FROM hosts').fetchall(), [('first',)])
                live.execute("INSERT INTO hosts VALUES ('second')")
                live.commit()
                deployment_output.refresh(config)
                with closing(sqlite3.connect(output / 'scheduler.sqlite')) as copy:
                    self.assertEqual(copy.execute('SELECT COUNT(*) FROM hosts').fetchone()[0], 2)
                self.assertEqual(live.execute('PRAGMA journal_mode').fetchone()[0], 'wal')

            (output / 'inventory.toml').write_text('changed copy')
            self.assertEqual((etc / 'inventory.toml').read_text(), 'schema = 2\n')
            (etc / 'projects/demo.toml').unlink()
            deployment_output.refresh(config)
            self.assertFalse((output / 'projects/demo.toml').exists())
            self.assertEqual((output / 'devices.json').read_text(), '{"devices":{}}\n')

    def test_missing_runtime_artifacts_are_reported_before_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            config = SimpleNamespace(runtime=runtime, organization_mode=False, etc=runtime / 'ci-runners/etc',
                                     control=runtime / 'parallel', is_controller=False,
                                     host={'roles': ['executor']})
            output = runtime / 'output'
            output.mkdir()
            (output / 'scheduler-health.json').write_text('{}')
            (output / 'devices.json').write_text('{}')
            result = deployment_output.refresh(config)
            self.assertFalse(result['files'])
            self.assertFalse(list(output.iterdir()))
            self.assertEqual(result['missing'], sorted([
                'inventory.toml', 'policy.toml', 'deployment.json', 'executor.sqlite', 'executor-health.json',
            ]))
