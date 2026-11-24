"""Internal bundle contract: corruption and exact scope never become a pass."""
from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import stat
import unittest
import zipfile
from unittest.mock import patch

from ci_runners import artifacts, bundles


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identity(**changes) -> bundles.Identity:
    return bundles.Identity(**{'repository_id': 101, 'repository': 'example/project',
                               'run_id': 202, 'run_attempt': 2, 'part_id': 'cpu',
                               'part_attempt': 1, 'sha': 'a' * 40, **changes})


def build(files=None, **changes):
    return bundles.build_part(identity(), files if files is not None else {
        'outcome.json': b'{"outcome":"red","run-attempt":2,"reason":"command failed"}',
        'summary.md': '### CPU\n失败\n'.encode('utf-8')},
        **{'producer': 'part-job', 'created_at': '2026-11-20T01:00:00Z',
           'expires_at': '2026-11-23T01:00:00Z', **changes})


def rewrite(bundle, change):
    reference, manifest, payload = bundle
    doc = json.loads(manifest)
    change(doc)
    raw = json.dumps(doc, sort_keys=True).encode('utf-8')
    return dataclasses.replace(reference, manifest_sha256=sha(raw)), raw, payload


def archive_bundle(bundle, entries):
    """Recompute digests, leaving only archive semantics wrong."""
    reference, manifest, _ = bundle
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as archive:
        for name, raw, mode in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            archive.writestr(info, raw)
    payload = buf.getvalue()
    reference, manifest, _ = rewrite(bundle, lambda doc: doc.update(size=len(payload), sha256=sha(payload)))
    return reference, manifest, payload


class PartBundles(unittest.TestCase):
    def refuses(self, bundle, code):
        with self.assertRaises(bundles.BundleError) as caught:
            bundles.read_part(*bundle)
        self.assertEqual(caught.exception.code, code)

    def test_round_trip_uses_existing_parser_and_keeps_red(self):
        bundle = build()
        with patch.object(artifacts, 'members', wraps=artifacts.members) as reader:
            outcome, summary = bundles.read_part(*bundle)
        reader.assert_called_once_with('ci-part-cpu-1', bundle[2])
        self.assertEqual(outcome['outcome'], 'red')
        self.assertEqual(outcome['reason'], 'command failed')
        self.assertIn('失败', summary)

    def test_byte_snapshots_are_deterministic_and_summary_is_optional(self):
        self.assertEqual(build(), build())
        inputs = {'outcome.json': b'{"outcome":"pass","run-attempt":2}', 'summary.md': b'summary'}
        self.assertEqual(build(inputs), build(dict(reversed(list(inputs.items())))))
        files = {'outcome.json': b'{"outcome":"pass","run-attempt":2}'}
        bundle = build(files)
        files['outcome.json'] = b'changed after preparation'
        self.assertEqual(bundles.read_part(*bundle), ({'outcome': 'pass', 'run-attempt': 2}, ''))
        self.assertEqual(json.loads(bundle[1])['files'][0]['sha256'], sha(b'{"outcome":"pass","run-attempt":2}'))

    def test_identity_validation_rejects_boolean_attempts_and_unsafe_ids(self):
        bad = {field: [True, False, 0, -1, 1.0, '1', None]
               for field in ('repository_id', 'run_id', 'run_attempt', 'part_attempt')}
        bad.update(repository=['../project', 'example/..', '/project', 'example/project/other',
                               'example\\project', [], None],
                   part_id=['plan', '../cpu', '/cpu', 'a/b', '', None],
                   sha=['a' * 39, 'A' * 40, '', None])
        bad['part_attempt'].append(100)
        for field, values in bad.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(bundles.BundleError):
                    identity(**{field: value})
        self.assertEqual(identity(part_attempt=99).part_attempt, 99)

    def test_exact_reference_rejects_other_repositories_runs_attempts_parts_and_shas(self):
        reference, manifest, payload = build()
        for field, value in {'repository_id': 102, 'repository': 'example/other', 'run_id': 203,
                             'run_attempt': 3, 'part_id': 'gpu', 'part_attempt': 2, 'sha': 'b' * 40}.items():
            with self.subTest(field=field):
                wrong = dataclasses.replace(reference, identity=identity(**{field: value}))
                self.refuses((wrong, manifest, payload), 'identity')

    def test_manifest_digest_and_control_size_are_required(self):
        reference, manifest, payload = build()
        self.refuses((reference, manifest + b' ', payload), 'digest')
        with patch.object(bundles, 'MAX_MANIFEST', len(manifest) - 1):
            self.refuses((reference, manifest, payload), 'size')
        for value in ('', 'a' * 63, 'A' * 64, 123, None):
            with self.subTest(value=value), self.assertRaises(bundles.BundleError):
                bundles.Reference(identity(), value)

    def test_manifest_schema_and_completion_are_not_inferred(self):
        bundle = build()
        for change, code in [({'schema': True}, 'format'), ({'schema': 2}, 'format'),
                             ({'backend': 'github'}, 'format'), ({'kind': 'plan'}, 'format'),
                             ({'unexpected': 1}, 'format'), ({'status': 'staging'}, 'incomplete'),
                             ({'created_at': 'yesterday'}, 'format'),
                             ({'expires_at': '2026-11-20T01:00:00Z'}, 'format'),
                             ({'producer': '../job'}, 'format')]:
            with self.subTest(change=change):
                self.refuses(rewrite(bundle, lambda d: d.update(change)), code)
        self.refuses(rewrite(bundle, lambda d: d.pop('status')), 'format')
        self.refuses(rewrite(bundle, lambda d: d['identity'].update(run_attempt=True)), 'identity')
        self.refuses(rewrite(bundle, lambda d: d['identity'].update(unknown=1)), 'identity')

    def test_ambiguous_or_malformed_json_is_rejected(self):
        reference, manifest, payload = build()
        for raw in (b'{', b'[]', b'\xff', b'{"schema":1,"schema":1}', b'{"x":NaN}',
                    b'{"x":Infinity}', b'[' * 2000):
            with self.subTest(raw=raw[:24]):
                self.refuses((dataclasses.replace(reference, manifest_sha256=sha(raw)), raw, payload), 'format')
        duplicate = manifest[:-1] + b',"schema":1}'
        self.refuses((dataclasses.replace(reference, manifest_sha256=sha(duplicate)), duplicate, payload), 'format')

    def test_manifest_file_list_is_bounded_and_exact(self):
        bundle = build()
        doc = json.loads(bundle[1])
        file = doc['files'][0]
        for files, code in [([], 'format'), ([file] * 3, 'format'), ([file, file], 'archive'),
                            ([{**file, 'name': '../outcome.json'}], 'archive'),
                            ([{**file, 'name': 'summary.md'}], 'format'),
                            ([{**file, 'size': True}], 'size'),
                            ([{**file, 'size': -1}], 'size'),
                            ([{**file, 'size': artifacts.MAX_FILE + 1}], 'size'),
                            ([{**file, 'sha256': 'bad'}], 'format'),
                            ([{**file, 'extra': 1}], 'format')]:
            with self.subTest(files=files):
                self.refuses(rewrite(bundle, lambda d: d.update(files=files)), code)
        one = build({'outcome.json': b'{"outcome":"pass","run-attempt":2}'})
        self.refuses(rewrite(one, lambda d: d.update(files=d['files'] * 2)), 'archive')

    def test_payload_sizes_and_both_levels_of_digest_are_checked(self):
        bundle = build()
        reference, manifest, payload = bundle
        self.refuses((reference, manifest, payload + b'x'), 'size')
        self.refuses((reference, manifest, payload[:-1] + b'x'), 'digest')
        self.refuses(rewrite(bundle, lambda d: d.update(size=True)), 'size')
        self.refuses(rewrite(bundle, lambda d: d.update(size=artifacts.MAX_ZIP + 1)), 'size')
        self.refuses(rewrite(bundle, lambda d: d['files'][0].update(sha256='0' * 64)), 'digest')
        self.refuses(rewrite(bundle, lambda d: d['files'][0].update(size=1)), 'size')

    def test_archive_members_must_be_unique_regular_files(self):
        bundle = build({'outcome.json': b'{"outcome":"pass","run-attempt":2}'})
        raw = b'{"outcome":"pass","run-attempt":2}'
        for entries in [[], [('outcome.json', raw, stat.S_IFLNK | 0o600)],
                        [('outcome.json', raw, stat.S_IFIFO | 0o600)],
                        [('outcome.json/', raw, stat.S_IFDIR | 0o700)],
                        [('../outcome.json', raw, stat.S_IFREG | 0o600)],
                        [('outcome.json', raw, 0), ('extra', b'', 0)]]:
            with self.subTest(entries=entries):
                self.refuses(archive_bundle(bundle, entries), 'archive')
        # Matching duplicate contents still do not authorize a duplicate member.
        with self.assertWarns(UserWarning):
            duplicate = archive_bundle(bundle, [('outcome.json', raw, 0)] * 2)
        self.refuses(duplicate, 'archive')
        # ZipInfo truncates a NUL-suffixed name. Reject that normalization too.
        hidden = archive_bundle(bundle, [('outcome.jsonX', raw, 0)])
        reference, manifest, payload = hidden
        nul = payload.replace(b'outcome.jsonX', b'outcome.json\x00')
        reference, manifest, _ = rewrite(hidden, lambda d: d.update(sha256=sha(nul)))
        self.refuses((reference, manifest, nul), 'archive')

    def test_bad_zip_and_crc_are_terminal(self):
        bundle = build()
        invalid = b'not a zip file'
        reference, manifest, _ = rewrite(bundle, lambda d: d.update(size=len(invalid), sha256=sha(invalid)))
        self.refuses((reference, manifest, invalid), 'archive')
        plain = archive_bundle(bundle, [('outcome.json', b'{"outcome":"red","run-attempt":2,"reason":"command failed"}', 0),
                                       ('summary.md', '### CPU\n失败\n'.encode('utf-8'), 0)])
        reference, manifest, payload = plain
        corrupted = payload.replace(b'command failed', b'command FAILED', 1)
        reference, manifest, _ = rewrite(plain, lambda d: d.update(sha256=sha(corrupted)))
        self.refuses((reference, manifest, corrupted), 'archive')

    def test_invalid_outcome_and_old_run_attempt_never_pass(self):
        for raw, code in [(b'{}', 'identity'), (b'[]', 'format'),
                          (b'{"outcome":"pass","run-attempt":1}', 'identity'),
                          (b'{"outcome":"pass","run-attempt":true}', 'identity'),
                          (b'{"outcome":"pass","run-attempt":2.0}', 'identity'),
                          (b'{"outcome":"pass","run-attempt":2,"run-attempt":2}', 'format'),
                          (b'{"outcome":"unknown","run-attempt":2}', 'format'),
                          (b'{"outcome":[],"run-attempt":2}', 'format')]:
            with self.subTest(raw=raw), self.assertRaises(bundles.BundleError) as caught:
                build({'outcome.json': raw})
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(bundles.BundleError) as caught:
            build({'outcome.json': b'{"outcome":"pass","run-attempt":2}', 'summary.md': b'\xff'})
        self.assertEqual(caught.exception.code, 'format')

    def test_producer_rejects_unsupported_files_and_oversize_bytes(self):
        for files in ({}, {'summary.md': b''}, {'../outcome.json': b'{}'},
                      {'outcome.json': b'{}', 'log': b''},
                      {'outcome.json': 'not bytes'},
                      {'outcome.json': b'x' * (artifacts.MAX_FILE + 1)}):
            with self.subTest(names=list(files)), patch.object(bundles.zipfile, 'ZipFile') as writer:
                with self.assertRaises(bundles.BundleError):
                    build(files)
                writer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
