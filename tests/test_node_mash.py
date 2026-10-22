import argparse
import contextlib
import io
import os
from pathlib import Path
import tempfile
import time
import sys
import errno
import ctypes
from unittest.mock import patch
import unittest
import node_mash as mash


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.dep = self.project / 'node_modules'
        self.dep.mkdir(parents=True)
        self.source = self.project / 'package.json'
        self.source.write_text('{}')
        self.args = argparse.Namespace(roots=[str(self.root)], store=str(self.root / 'store'), command='dedupe', apply=False, days=365, purge=False, min_bytes=0, method='hardlink')

    def run_mash(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return mash.run(self.args)

    def test_activity_ignores_dependency_and_junk(self):
        old = time.time() - 400 * 86400
        os.utime(self.source, (old, old))
        (self.dep / 'fresh.js').write_text('new')
        (self.project / '.DS_Store').write_text('new')
        self.assertEqual(mash.activity(self.project, self.root / 'store'), old)
        self.source.write_text('updated')
        self.assertGreater(mash.activity(self.project, self.root / 'store'), old)

    def test_purge_requires_apply_and_preserves_source(self):
        old = time.time() - 400 * 86400
        os.utime(self.source, (old, old))
        self.args.command = 'purge'
        self.assertFalse(self.run_mash()[0]['applied'])
        self.assertTrue(self.dep.exists())
        self.args.apply = True
        self.assertTrue(self.run_mash()[0]['applied'])
        self.assertFalse(self.dep.exists())
        self.assertTrue(self.source.exists())

    def test_empty_project_is_not_purged(self):
        self.source.unlink()
        self.args.command = 'purge'
        self.args.apply = True
        self.assertEqual(self.run_mash(), [])
        self.assertTrue(self.dep.exists())

    def test_only_identical_contents_and_modes_link(self):
        a, b, c, d = [self.dep / x for x in ['a', 'b', 'c', 'd']]
        a.write_text('same'); b.write_text('same'); c.write_text('diff'); d.write_text('same')
        d.chmod(0o755)
        self.assertEqual(len(self.run_mash()), 1)
        self.assertFalse(a.samefile(b))
        self.args.apply = True
        self.run_mash()
        self.assertTrue(a.samefile(b))
        self.assertFalse(a.samefile(c))
        self.assertFalse(a.samefile(d))

    def test_symlink_store_survives_project_removal(self):
        (self.dep / 'a').write_text('same')
        other = self.root / 'other' / 'node_modules'
        other.mkdir(parents=True)
        (other / 'b').write_text('same')
        self.args.method = 'symlink'
        self.args.apply = True
        actions = self.run_mash()
        target = Path(actions[0]['path'])
        self.assertTrue(target.is_symlink())
        Path(actions[0]['source']).unlink()
        self.assertEqual(target.read_text(), 'same')

    def test_report_combined_does_not_double_count_purged_duplicates(self):
        (self.dep / 'a').write_text('same')
        (self.dep / 'b').write_text('same')
        old = time.time() - 400 * 86400
        os.utime(self.source, (old, old))
        self.args.command = 'service'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            mash.run(self.args)
        import json
        summary = json.loads(output.getvalue())['summary']
        self.assertEqual(summary['estimated_purge_bytes'], 8)
        self.assertEqual(summary['estimated_dedupe_bytes'], 4)
        self.assertEqual(summary['estimated_combined_bytes'], 8)
        self.assertTrue((self.dep / 'a').exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS clonefile integration')
    def test_clone_writes_and_deletes_are_private_and_metadata_preserved(self):
        a, b = self.dep / 'a', self.dep / 'b'
        a.write_bytes(b'x' * 65536)
        b.write_bytes(b'x' * 65536)
        old = time.time_ns() - 100000000000
        os.utime(b, ns=(old, old))
        libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        setter = libc.setxattr
        setter.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        self.assertEqual(setter(os.fsencode(a), b'com.node-mash.source', b'source-only', 11, 0, 0), 0)
        self.assertEqual(setter(os.fsencode(b), b'com.node-mash.test', b'target-only', 11, 0, 0), 0)
        self.args.method = 'clone'
        self.args.apply = True
        result = self.run_mash()
        self.assertTrue(result[0]['applied'], result)
        self.assertFalse(a.samefile(b))
        self.assertFalse(b.is_symlink())
        self.assertEqual(b.stat().st_mtime_ns, old)
        self.assertIn(b'com.node-mash.test', mash.xattr_names(libc, b))
        self.assertNotIn(b'com.node-mash.source', mash.xattr_names(libc, b))
        getter = libc.getxattr
        getter.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        value = ctypes.create_string_buffer(11)
        self.assertEqual(getter(os.fsencode(b), b'com.node-mash.test', value, 11, 0, 0), 11)
        self.assertEqual(value.raw, b'target-only')
        with b.open('r+b') as stream:
            stream.write(b'updated')
        self.assertEqual(a.read_bytes(), b'x' * 65536)
        with a.open('r+b') as stream:
            stream.write(b'original')
        self.assertEqual(b.read_bytes()[:7], b'updated')
        a.unlink()
        self.assertEqual(b.read_bytes()[:7], b'updated')

    def test_clone_unsupported_preserves_original_without_fallback(self):
        a, b = self.dep / 'a', self.dep / 'b'
        a.write_text('same'); b.write_text('same')
        before = b.stat().st_ino
        self.args.method = 'clone'
        self.args.apply = True
        with patch.object(mash, 'clone_replace', side_effect=OSError(errno.ENOTSUP, 'unsupported')):
            result = self.run_mash()
        self.assertFalse(result[0]['applied'])
        self.assertIn('clone unavailable', result[0]['skipped'])
        self.assertEqual(b.stat().st_ino, before)
        self.assertEqual(b.read_text(), 'same')
        self.assertFalse(a.samefile(b))

    def test_pycache_discovered_and_deduped_but_not_purged(self):
        cache = self.project / '__pycache__'
        cache.mkdir()
        a, b = cache / 'a.cpython-313.pyc', cache / 'b.cpython-313.pyc'
        a.write_bytes(b'identical bytecode')
        b.write_bytes(b'identical bytecode')
        self.assertIn(cache, mash.discover([self.root], self.root / 'store'))
        old = time.time() - 400 * 86400
        os.utime(self.source, (old, old))
        self.assertEqual(mash.activity(self.project, self.root / 'store'), old)
        actions = self.run_mash()
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]['path'], str(b))
        self.args.command = 'purge'
        self.args.apply = True
        self.run_mash()
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())

    def test_pycache_inside_environment_not_discovered_twice(self):
        cache = self.dep / 'python' / '__pycache__'
        cache.mkdir(parents=True)
        self.assertEqual(mash.discover([self.root], self.root / 'store'), [self.dep])

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS clonefile integration')
    def test_corruption_after_rename_aborts_before_next_file(self):
        self.assert_abort_after_one_replacement('content')

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS clonefile integration')
    def test_metadata_corruption_after_rename_aborts_before_next_file(self):
        self.assert_abort_after_one_replacement('mtime')

    def assert_abort_after_one_replacement(self, corruption):
        files = [self.dep / name for name in ['a', 'b', 'c']]
        for file in files:
            file.write_bytes(b'original contents')
        inodes = {file: file.stat().st_ino for file in files}
        self.args.method = 'clone'
        self.args.apply = True
        original_replace = os.replace
        replaced = []
        def corrupt_after_rename(source, target):
            original_replace(source, target)
            replaced.append(Path(target))
            if corruption == 'content':
                Path(target).write_bytes(b'corrupted')
            else:
                os.utime(target, ns=(1, 1))
        with patch.object(mash.os, 'replace', side_effect=corrupt_after_rename):
            with self.assertRaisesRegex(RuntimeError, 'POST-REPLACEMENT VERIFICATION FAILED'):
                self.run_mash()
        self.assertEqual(len(replaced), 1)
        for file in files:
            if file != replaced[0]:
                self.assertEqual(file.stat().st_ino, inodes[file])
                self.assertEqual(file.read_bytes(), b'original contents')

    def test_purge_verification_failure_stops_next_directory(self):
        other = self.project / '.venv'
        other.mkdir()
        (other / 'pyvenv.cfg').write_text('home = /python')
        old = time.time() - 400 * 86400
        os.utime(self.source, (old, old))
        self.args.command = 'purge'
        self.args.apply = True
        with patch.object(mash.shutil, 'rmtree') as remove:
            with self.assertRaisesRegex(RuntimeError, 'Purge verification failed'):
                self.run_mash()
        self.assertEqual(remove.call_count, 1)
        self.assertTrue(other.exists())
        self.assertTrue(self.dep.exists())

    def test_service_exits_on_failure_without_waiting_or_retrying(self):
        with patch.object(sys, 'argv', ['node-mash', 'service', str(self.root), '--apply']):
            with patch.object(mash, 'run', side_effect=RuntimeError('verification failed')) as run:
                with patch.object(mash.time, 'sleep') as sleep:
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as error:
                            mash.main()
                    self.assertEqual(error.exception.code, 1)
                    self.assertEqual(run.call_count, 1)
                    sleep.assert_not_called()

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS clonefile integration')
    def test_disappearance_before_clone_skipped_and_next_file_processed(self):
        for name in ['a', 'b', 'c']:
            (self.dep / name).write_bytes(b'identical')
        self.args.method = 'clone'
        self.args.apply = True
        actual_clone = mash.clone_replace
        calls = []
        def remove_first(source, target):
            calls.append(target)
            if len(calls) == 1:
                target.unlink()
                raise FileNotFoundError(errno.ENOENT, 'removed externally', str(target))
            return actual_clone(source, target)
        with patch.object(mash, 'clone_replace', side_effect=remove_first):
            actions = self.run_mash()
        self.assertEqual(len(calls), 2)
        self.assertIn('disappeared', actions[0]['skipped'])
        self.assertTrue(actions[1]['verified'])

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS clonefile integration')
    def test_disappearance_after_replacement_aborts(self):
        for name in ['a', 'b', 'c']:
            (self.dep / name).write_bytes(b'identical')
        self.args.method = 'clone'
        self.args.apply = True
        replace = os.replace
        calls = []
        def remove_after(source, target):
            replace(source, target)
            calls.append(target)
            target.unlink()
        with patch.object(mash.os, 'replace', side_effect=remove_after):
            with self.assertRaisesRegex(RuntimeError, 'POST-REPLACEMENT VERIFICATION FAILED'):
                self.run_mash()
        self.assertEqual(len(calls), 1)

    def test_metadata_exception_only_allows_provenance_differences(self):
        original = {'mode': 0o644, 'xattrs': {b'com.apple.quarantine': b'keep'}}
        added = {'mode': 0o644, 'xattrs': {b'com.apple.quarantine': b'keep', b'com.apple.provenance': b'new'}}
        self.assertTrue(mash.target_metadata_matches(added, original))
        for actual in [
            {'mode': 0o755, 'xattrs': added['xattrs']},
            {'mode': 0o644, 'xattrs': {b'com.apple.provenance': b'new'}},
            {'mode': 0o644, 'xattrs': {b'com.apple.quarantine': b'changed'}},
            {'mode': 0o644, 'xattrs': {**added['xattrs'], b'other': b'new'}},
        ]:
            self.assertFalse(mash.target_metadata_matches(actual, original))
        existing = {'mode': 0o644, 'xattrs': {b'com.apple.provenance': b'original'}}
        self.assertTrue(mash.target_metadata_matches({'mode': 0o644, 'xattrs': {}}, existing))
        self.assertTrue(mash.target_metadata_matches({'mode': 0o644, 'xattrs': {b'com.apple.provenance': b'changed'}}, existing))

    def test_reports_written_to_configured_directory_with_private_permissions(self):
        import json
        self.args.report_dir = str(self.root / 'reports')
        self.run_mash()
        self.run_mash()
        reports = list((self.root / 'reports').glob('*.json'))
        self.assertEqual(len(reports), 2)
        self.assertEqual(len(list((self.root / 'reports').glob('*.log'))), 2)
        self.assertEqual(json.loads(reports[0].read_text())['status'], 'completed')
        self.assertEqual(reports[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.project.glob('*.json')), [self.source])

    def test_failed_run_writes_partial_report_and_preserves_journal(self):
        import json
        self.args.report_dir = str(self.root / 'reports')
        def fail(args):
            print(json.dumps({'event': 'verified', 'path': 'example'}), file=sys.stderr)
            raise RuntimeError('verification failed')
        with patch.object(mash, '_run', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                self.run_mash()
        report = json.loads(next((self.root / 'reports').glob('*.json')).read_text())
        self.assertEqual(report['status'], 'aborted')
        self.assertEqual(report['events'][0]['event'], 'verified')
        self.assertTrue(Path(report['progress_log']).exists())

    def test_cli_default_report_directory_is_outside_checkout(self):
        with patch.object(sys, 'argv', ['node-mash', 'dedupe', str(self.root)]):
            with patch.object(mash, 'run') as run:
                mash.main()
        self.assertEqual(run.call_args.args[0].report_dir, '~/.local/state/node-mash/reports')

    def test_report_directory_excluded_from_activity(self):
        reports = self.project / 'reports'
        reports.mkdir()
        (reports / 'new.log').write_text('new')
        old = time.time() - 400 * 86400
        os.utime(self.source, (old, old))
        self.assertEqual(mash.activity(self.project, self.root / 'store', reports), old)

    def test_service_config_supplies_roots_and_cli_overrides(self):
        import json
        config = self.root / 'service.json'
        config.write_text(json.dumps({'roots': [str(self.root)], 'days': 90, 'apply': True, 'purge': True}))
        with patch.object(sys, 'argv', ['node-mash', 'service', '--once', '--config', str(config), '--days', '180', '--dry-run']):
            with patch.object(mash, 'run') as run:
                mash.main()
        args = run.call_args.args[0]
        self.assertEqual(args.roots, [str(self.root)])
        self.assertEqual(args.days, 180)
        self.assertFalse(args.apply)
        self.assertTrue(args.purge)
        with patch.object(sys, 'argv', ['node-mash', 'service', str(self.project), '--once', '--config', str(config)]):
            with patch.object(mash, 'run') as run:
                mash.main()
        self.assertEqual(run.call_args.args[0].roots, [str(self.project)])

    def test_invalid_configs_rejected(self):
        import json
        config = self.root / 'service.json'
        for payload in [{'roots': []}, {'apply': 'false'}, {'purge': 1}, {'days': -1},
                        {'days': float('nan')}, {'report_dir': ''}, {'method': 'copy'},
                        {'min_bytes': 0.5}, {'unexpected': True}, []]:
            config.write_text(json.dumps(payload))
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                mash.load_config(config)

    def test_service_requires_roots_or_valid_config_before_work(self):
        for extra in [[], ['--config', str(self.root / 'missing.json')]]:
            with patch.object(sys, 'argv', ['node-mash', 'service', '--once', *extra]):
                with patch.object(mash, 'run') as run, contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        mash.main()
                    self.assertEqual(error.exception.code, 2)
                    run.assert_not_called()

    def test_python_environment_discovery(self):
        env = self.project / '.venv'
        env.mkdir()
        (env / 'pyvenv.cfg').write_text('home = /python')
        self.assertEqual(set(mash.discover([self.root], self.root / 'store')), {self.dep, env})


if __name__ == '__main__':
    unittest.main()
