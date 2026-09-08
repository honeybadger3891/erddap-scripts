import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import erddapctl as ctl
import erddap_admin_rename as core
from erddap_site import create_profile


XML = b'<erddapDatasets><dataset type="EDDGridFromNcFiles" datasetID="lake"><addAttributes><att name="title">Lake Ontario waves</att></addAttributes></dataset></erddapDatasets>'


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'datasets.xml'
        self.source.write_bytes(XML)
        self.workspace = self.root / 'changes'
        self.big_parent = self.root / 'data'
        (self.big_parent / 'flag').mkdir(parents=True)
        self.profile = self.root / 'site.json'
        create_profile(self.profile, [str(self.source)], str(self.workspace), big_parent=str(self.big_parent),
                       server='https://example.test/erddap')

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = ctl.main(list(map(str, args)))
        return status, output.getvalue()

    def scan(self, old='Lake Ontario', new='Lake of America', *extra):
        status, output = self.run_cli('rename', '--profile', self.profile, '--old', old, '--new', new,
                                     '--scan-only', *extra)
        self.assertEqual(status, 0, output)
        return sorted(self.workspace.iterdir())[-1]

    def action(self, action, job, approve=True):
        with mock.patch.object(ctl, 'require_terminal'), mock.patch.object(ctl, 'confirm', return_value=approve):
            return self.run_cli(action, '--job', job)

    def test_scan_only_stores_private_job_and_never_updates(self):
        job = self.scan()
        self.assertEqual(self.source.read_bytes(), XML)
        self.assertTrue((job / 'plan.json').exists())
        self.assertEqual((job / 'job.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(job.stat().st_mode & 0o777, 0o700)
        self.assertFalse((job / 'backups').exists())
        status, output = self.run_cli('history', '--profile', self.profile)
        self.assertEqual(status, 0)
        self.assertIn('ready', output)
        self.assertIn('Lake Ontario', output)

    def test_apply_decline_and_eof_leave_configuration_unchanged(self):
        job = self.scan()
        self.assertEqual(self.action('apply', job, False)[0], 0)
        with mock.patch.object(ctl, 'require_terminal'), mock.patch.object(ctl, 'confirm', side_effect=EOFError):
            status, _ = self.run_cli('apply', '--job', job)
        self.assertEqual(status, 130)
        self.assertEqual(self.source.read_bytes(), XML)
        self.assertFalse((job / 'backups').exists())

    def test_apply_requires_terminal(self):
        job = self.scan()
        with mock.patch.object(ctl.sys.stdin, 'isatty', return_value=False):
            status, output = self.run_cli('apply', '--job', job)
        self.assertEqual(status, 1)
        self.assertIn('terminal', output)
        self.assertEqual(self.source.read_bytes(), XML)

    def test_typed_confirmation_defaults_to_cancel(self):
        with mock.patch.object(ctl, 'require_terminal'), mock.patch('builtins.input', return_value=''):
            self.assertFalse(ctl.confirm('apply', 'a' * 64))
        with mock.patch.object(ctl, 'require_terminal'), mock.patch('builtins.input', return_value='APPLY ' + 'a' * 12):
            self.assertTrue(ctl.confirm('apply', 'a' * 64))

    def test_apply_reload_rollback_and_history(self):
        job = self.scan()
        status, output = self.action('apply', job)
        self.assertEqual(status, 0, output)
        self.assertIn(b'Lake of America', self.source.read_bytes())
        self.assertFalse(list((self.big_parent / 'flag').iterdir()))
        self.assertIn('applied', self.run_cli('history', '--profile', self.profile)[1])
        self.assertEqual(self.action('reload', job)[0], 0)
        self.assertTrue((self.big_parent / 'flag' / 'lake').exists())
        self.assertEqual(self.action('rollback', job)[0], 0)
        self.assertEqual(self.source.read_bytes(), XML)
        self.assertIn('rolled_back', self.run_cli('history', '--profile', self.profile)[1])

    def test_plan_changed_during_confirmation_is_rejected(self):
        job = self.scan()
        def mutate(action, digest):
            path = job / 'plan.json'
            path.write_bytes(path.read_bytes() + b'\n')
            return True
        with mock.patch.object(ctl, 'require_terminal'), mock.patch.object(ctl, 'confirm', side_effect=mutate):
            status, output = self.run_cli('apply', '--job', job)
        self.assertEqual(status, 1, output)
        self.assertIn('Confirmation', output)
        self.assertEqual(self.source.read_bytes(), XML)

    def test_job_changed_during_confirmation_is_rejected(self):
        job = self.scan()
        def mutate(action, digest):
            path = job / 'job.json'
            path.write_bytes(path.read_bytes() + b'\n')
            return True
        with mock.patch.object(ctl, 'require_terminal'), mock.patch.object(ctl, 'confirm', side_effect=mutate):
            status, output = self.run_cli('apply', '--job', job)
        self.assertEqual(status, 1, output)
        self.assertIn('settings changed', output)
        self.assertEqual(self.source.read_bytes(), XML)

    def test_profile_edit_does_not_redirect_existing_reload(self):
        job = self.scan()
        self.assertEqual(self.action('apply', job)[0], 0)
        other = self.root / 'other'
        (other / 'flag').mkdir(parents=True)
        profile = json.loads(self.profile.read_text())
        profile['big_parent'] = str(other)
        self.profile.write_bytes(core.encoded(profile))
        self.assertEqual(self.action('reload', job)[0], 0)
        self.assertTrue((self.big_parent / 'flag' / 'lake').exists())
        self.assertFalse(list((other / 'flag').iterdir()))

    def test_unrelated_manifest_cannot_be_reloaded_as_this_job(self):
        job = self.scan()
        self.assertEqual(self.action('apply', job)[0], 0)
        manifest_path = next((job / 'backups').glob('*/manifest.json'))
        manifest = json.loads(manifest_path.read_text())
        manifest['plan_sha256'] = '0' * 64
        manifest_path.write_bytes(core.encoded(manifest))
        status, output = self.action('reload', job)
        self.assertEqual(status, 1, output)
        self.assertIn('does not belong', output)
        self.assertFalse(list((self.big_parent / 'flag').iterdir()))

    def test_incomplete_public_audit_cannot_be_guided_applied(self):
        audit = {'server': 'https://example.test/erddap', 'dataset_count': 0, 'datasets': [], 'matches': [],
                 'errors': [{'message': 'timeout'}], 'complete': False, 'limitations': []}
        with mock.patch('erddap_catalog.audit_catalog', return_value=audit):
            status, output = self.run_cli('rename', '--profile', self.profile, '--old', 'Lake Ontario',
                                         '--new', 'Lake of America', '--scan-only', '--audit-public')
        self.assertEqual(status, 1, output)
        job = next(self.workspace.iterdir())
        self.assertIn('audit_incomplete', self.run_cli('history', '--profile', self.profile)[1])
        self.assertEqual(self.action('apply', job)[0], 1)
        self.assertEqual(self.source.read_bytes(), XML)

    def test_zero_change_job_is_not_claimed_applied(self):
        job = self.scan('Atlantic', 'Pacific')
        status, output = self.action('apply', job)
        self.assertEqual(status, 0, output)
        self.assertIn('No eligible changes', output)
        self.assertIn('no_changes', self.run_cli('history', '--profile', self.profile)[1])
        self.assertFalse((job / 'backups').exists())

    def test_history_reports_corrupt_and_interrupted_jobs(self):
        job = self.scan()
        saved = json.loads((job / 'job.json').read_text())
        saved['scan_status'] = 'scan_interrupted'
        (job / 'job.json').write_bytes(core.encoded(saved))
        self.assertIn('scan_interrupted', self.run_cli('history', '--profile', self.profile)[1])
        (job / 'job.json').write_bytes(b'invalid')
        self.assertIn('NEEDS REVIEW', self.run_cli('history', '--profile', self.profile)[1])

    def test_failed_scan_diagnostics_are_kept_after_the_session(self):
        with mock.patch.object(core, 'build_plan', side_effect=core.PlanError('source changed during scan')):
            status, output = self.run_cli('rename', '--profile', self.profile, '--old', 'Lake Ontario',
                                         '--new', 'Lake of America', '--scan-only')
        self.assertEqual(status, 1, output)
        job = next(self.workspace.iterdir())
        self.assertIn('source changed during scan', (job / 'scan.log').read_text())
        self.assertIn('scan_failed', self.run_cli('history', '--profile', self.profile)[1])
        self.assertEqual(self.source.read_bytes(), XML)

    def test_saved_plan_names_and_site_must_match_job(self):
        job = self.scan()
        saved = json.loads((job / 'job.json').read_text())
        saved['old'] = 'Unexpected'
        (job / 'job.json').write_bytes(core.encoded(saved))
        status, output = self.action('apply', job)
        self.assertEqual(status, 1, output)
        self.assertIn('names do not match', output)
        self.assertEqual(self.source.read_bytes(), XML)


if __name__ == '__main__':
    unittest.main()
