import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import erddap_admin_rename as admin


XML = b'''<?xml version="1.0" encoding="UTF-8"?>\r\n<erddapDatasets>\r\n  <!-- preserve Lake Ontario historical note -->\r\n  <dataset type="EDDGridFromNcFiles" datasetID="lake">\r\n    <addAttributes><att name="title">Lake Ontario waves</att></addAttributes>\r\n  </dataset>\r\n</erddapDatasets>\r\n'''


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # macOS /var is a symlink; use canonical fixture paths.
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "datasets.xml"
        self.source.write_bytes(XML)
        self.source.chmod(0o640)
        self.plan = self.root / "plan.json"
        self.backups = self.root / "backups"

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = admin.main(list(map(str, args)))
        return status, output.getvalue()

    def scan(self, *extra):
        status, output = self.run_cli("scan", "--datasets", self.source,
                                      "--old", "Lake Ontario", "--new", "Lake of America",
                                      "--plan", self.plan, *extra)
        self.assertEqual(status, 0, output)
        return admin.sha256(self.plan.read_bytes())

    def apply(self):
        digest = self.scan()
        status, output = self.run_cli("apply", "--plan", self.plan, "--confirm", digest,
                                     "--backup-dir", self.backups)
        self.assertEqual(status, 0, output)
        return next(self.backups.glob("*/manifest.json"))

    def test_scan_and_review_are_read_only_and_report_exact_values(self):
        self.scan()
        self.assertEqual(self.source.read_bytes(), XML)
        self.assertEqual(stat.S_IMODE(self.plan.stat().st_mode), 0o600)
        report = Path(str(self.plan) + ".report.txt")
        self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
        status, output = self.run_cli("review", "--plan", self.plan)
        self.assertEqual(status, 0)
        self.assertIn("Lake Ontario waves", output)
        self.assertIn("Lake of America waves", output)
        self.assertIn("lake", output)
        self.assertEqual(self.source.read_bytes(), XML)
        self.assertFalse(self.backups.exists())

    def test_default_scan_flags_do_not_apply(self):
        status, output = self.run_cli("--datasets", self.source, "--old", "Lake Ontario", "--new", "new")
        self.assertEqual(status, 0, output)
        self.assertEqual(self.source.read_bytes(), XML)
        self.assertFalse(self.plan.exists())

    def test_apply_and_rollback_preserve_bytes_permissions_and_backups(self):
        manifest_path = self.apply()
        self.assertEqual(self.source.read_bytes(), XML.replace(b"Lake Ontario waves", b"Lake of America waves"))
        self.assertEqual(stat.S_IMODE(self.source.stat().st_mode), 0o640)
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["status"], "applied")
        backup = Path(manifest["files"][0]["backup"])
        self.assertEqual(backup.read_bytes(), XML)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        status, output = self.run_cli("rollback", "--manifest", manifest_path,
                                     "--confirm", admin.sha256(manifest_path.read_bytes()))
        self.assertEqual(status, 0, output)
        self.assertEqual(self.source.read_bytes(), XML)
        self.assertEqual(stat.S_IMODE(self.source.stat().st_mode), 0o640)
        self.assertEqual(json.loads(manifest_path.read_text())["status"], "rolled_back")

    def test_wrong_confirmation_leaves_no_backups_or_writes(self):
        self.scan()
        status, output = self.run_cli("apply", "--plan", self.plan, "--confirm", "yes", "--backup-dir", self.backups)
        self.assertEqual(status, 1)
        self.assertIn("Confirmation", output)
        self.assertFalse(self.backups.exists())
        self.assertEqual(self.source.read_bytes(), XML)

    @unittest.skipUnless(sys.platform != "darwin" and hasattr(os, "setxattr"), "non-Darwin extended attributes unavailable")
    def test_extended_attributes_survive_apply_and_rollback(self):
        key = "com.erddap.rename-test" if sys.platform == "darwin" else "user.erddap-rename-test"
        try:
            os.setxattr(self.source, key, b"original attribute")
        except OSError as error:
            self.skipTest("fixture filesystem does not support user xattrs: %s" % error)
        manifest_path = self.apply()
        self.assertEqual(os.getxattr(self.source, key), b"original attribute")
        status, output = self.run_cli("rollback", "--manifest", manifest_path,
                                     "--confirm", admin.sha256(manifest_path.read_bytes()))
        self.assertEqual(status, 0, output)
        self.assertEqual(os.getxattr(self.source, key), b"original attribute")

    def test_stale_source_blocks_apply(self):
        digest = self.scan()
        self.source.write_bytes(XML + b"\n")
        status, output = self.run_cli("apply", "--plan", self.plan, "--confirm", digest, "--backup-dir", self.backups)
        self.assertEqual(status, 1)
        self.assertEqual(self.source.read_bytes(), XML + b"\n")
        self.assertFalse(self.backups.exists())

    def test_changed_permissions_block_apply(self):
        digest = self.scan()
        self.source.chmod(0o600)
        status, _ = self.run_cli("apply", "--plan", self.plan, "--confirm", digest, "--backup-dir", self.backups)
        self.assertEqual(status, 1)
        self.assertEqual(self.source.read_bytes(), XML)
        self.assertFalse(self.backups.exists())

    def test_edited_plan_is_recomputed_even_with_updated_confirmation(self):
        self.scan()
        plan = json.loads(self.plan.read_text())
        plan["metadata"]["changes"] = []
        self.plan.write_bytes(admin.encoded(plan))
        status, output = self.run_cli("apply", "--plan", self.plan, "--confirm", admin.sha256(self.plan.read_bytes()), "--backup-dir", self.backups)
        self.assertEqual(status, 1)
        self.assertIn("stale or edited", output)
        self.assertEqual(self.source.read_bytes(), XML)

    def test_reapplying_a_plan_fails(self):
        self.apply()
        status, _ = self.run_cli("apply", "--plan", self.plan, "--confirm", admin.sha256(self.plan.read_bytes()), "--backup-dir", self.backups)
        self.assertEqual(status, 1)
        self.assertEqual(len(list(self.backups.iterdir())), 1)

    def test_backup_failure_prevents_source_writes(self):
        digest = self.scan()
        original = admin.write_private
        def fail_backup(path, data):
            if str(path).endswith(".original"):
                raise OSError("simulated backup failure")
            return original(path, data)
        with mock.patch.object(admin, "write_private", side_effect=fail_backup):
            status, output = self.run_cli("apply", "--plan", self.plan, "--confirm", digest, "--backup-dir", self.backups)
        self.assertEqual(status, 1)
        self.assertIn("backup failure", output)
        self.assertEqual(self.source.read_bytes(), XML)

    def test_second_file_failure_restores_first_file(self):
        second = self.root / "second.xml"
        second.write_bytes(XML.replace(b'datasetID="lake"', b'datasetID="second"'))
        digest = self.scan("--datasets", second)
        original_replace = admin.replace_staged
        def fail_second(staged, path):
            if Path(path) == second:
                raise OSError("simulated second-file failure")
            return original_replace(staged, path)
        with mock.patch.object(admin, "replace_staged", side_effect=fail_second):
            status, output = self.run_cli("apply", "--plan", self.plan, "--confirm", digest, "--backup-dir", self.backups)
        self.assertEqual(status, 1, output)
        self.assertEqual(self.source.read_bytes(), XML)
        self.assertEqual(second.read_bytes(), XML.replace(b'datasetID="lake"', b'datasetID="second"'))
        manifest = json.loads(next(self.backups.glob("*/manifest.json")).read_text())
        self.assertEqual(manifest["status"], "rolled_back_after_failure")
        self.assertEqual(list(self.root.glob(".*.rename-*")), [])

    def test_corrupt_backup_blocks_all_rollback_changes(self):
        manifest_path = self.apply()
        manifest = json.loads(manifest_path.read_text())
        Path(manifest["files"][0]["backup"]).write_bytes(b"corrupt")
        before = self.source.read_bytes()
        status, output = self.run_cli("rollback", "--manifest", manifest_path, "--confirm", admin.sha256(manifest_path.read_bytes()))
        self.assertEqual(status, 1)
        self.assertIn("Backup checksum", output)
        self.assertEqual(self.source.read_bytes(), before)

    def test_later_edit_blocks_rollback_and_reload(self):
        manifest_path = self.apply()
        self.source.write_bytes(self.source.read_bytes() + b"\n")
        before = self.source.read_bytes()
        big_parent = self.root / "erddapData"
        (big_parent / "flag").mkdir(parents=True)
        for command, extra in (("rollback", []), ("reload", ["--big-parent", big_parent])):
            status, output = self.run_cli(command, "--manifest", manifest_path,
                                         "--confirm", admin.sha256(manifest_path.read_bytes()), *extra)
            self.assertEqual(status, 1, output)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list((big_parent / "flag").iterdir()), [])

    def test_manifest_symlink_is_rejected_without_splitting_journal(self):
        manifest_path = self.apply()
        alias = self.root / "current-manifest.json"
        alias.symlink_to(manifest_path)
        source_before = self.source.read_bytes()
        manifest_before = manifest_path.read_bytes()
        status, output = self.run_cli("rollback", "--manifest", alias,
                                     "--confirm", admin.sha256(manifest_before))
        self.assertEqual(status, 1, output)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(self.source.read_bytes(), source_before)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)

    def test_reload_is_separate_and_targets_only_affected_ids(self):
        manifest_path = self.apply()
        big_parent = self.root / "erddapData"
        flags = big_parent / "flag"
        flags.mkdir(parents=True)
        self.assertEqual(list(flags.iterdir()), [])
        status, output = self.run_cli("reload", "--manifest", manifest_path, "--big-parent", big_parent,
                                     "--confirm", admin.sha256(manifest_path.read_bytes()))
        self.assertEqual(status, 0, output)
        self.assertEqual([p.name for p in flags.iterdir()], ["lake"])
        self.assertFalse((big_parent / "hardFlag").exists())
        self.assertIn("not proof", output)

    def test_reload_rejects_path_traversal_and_symlink_flag(self):
        manifest_path = self.apply()
        big_parent = self.root / "erddapData"
        flags = big_parent / "flag"
        flags.mkdir(parents=True)
        sentinel = self.root / "sentinel"
        sentinel.write_bytes(b"keep")
        (flags / "lake").symlink_to(sentinel)
        status, _ = self.run_cli("reload", "--manifest", manifest_path, "--big-parent", big_parent,
                                "--confirm", admin.sha256(manifest_path.read_bytes()))
        self.assertEqual(status, 1)
        self.assertEqual(sentinel.read_bytes(), b"keep")
        manifest = json.loads(manifest_path.read_text())
        manifest["reload_ids"] = ["../../sentinel"]
        manifest_path.write_bytes(admin.encoded(manifest))
        status, _ = self.run_cli("reload", "--manifest", manifest_path, "--big-parent", big_parent,
                                "--confirm", admin.sha256(manifest_path.read_bytes()))
        self.assertEqual(status, 1)
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_scan_does_not_overwrite_existing_plan(self):
        self.scan()
        before = self.plan.read_bytes()
        status, _ = self.run_cli("scan", "--datasets", self.source, "--old", "Lake Ontario", "--new", "another", "--plan", self.plan)
        self.assertEqual(status, 1)
        self.assertEqual(self.plan.read_bytes(), before)

    def test_partial_public_audit_is_reported_with_nonzero_exit(self):
        audit = {"server": "https://example.test/erddap", "dataset_count": 1, "datasets": [],
                 "matches": [], "errors": [{"error": "timeout"}], "complete": False, "limitations": []}
        with mock.patch("erddap_catalog.audit_catalog", return_value=audit):
            status, output = self.run_cli("scan", "--datasets", self.source, "--old", "Lake Ontario", "--new", "Lake of America",
                                         "--server", audit["server"], "--plan", self.plan)
        self.assertEqual(status, 1)
        self.assertIn("timeout", output)
        self.assertEqual(self.source.read_bytes(), XML)
        self.assertTrue(self.plan.exists())

    def test_symlink_and_hardlink_sources_fail_closed(self):
        linked = self.root / "link.xml"
        linked.symlink_to(self.source)
        with self.assertRaises(admin.AdminError):
            admin.read_regular(linked)
        linked.unlink()
        os.link(self.source, linked)
        status, output = self.run_cli("scan", "--datasets", self.source, "--old", "Lake Ontario", "--new", "Lake of America")
        self.assertEqual(status, 1, output)
        self.assertEqual(self.source.read_bytes(), XML)

    @unittest.skipUnless(sys.platform == "darwin", "native macOS ACL behavior")
    def test_native_macos_acl_is_refused_without_removing_access(self):
        subprocess.run(["chmod", "+a", "everyone allow read", str(self.source)], check=True)
        status, output = self.run_cli("scan", "--datasets", self.source,
                                     "--old", "Lake Ontario", "--new", "Lake of America", "--plan", self.plan)
        self.assertEqual(status, 1, output)
        self.assertIn("ACL", output)
        self.assertFalse(self.plan.exists())
        self.assertEqual(self.source.read_bytes(), XML)
        listing = subprocess.check_output(["/bin/ls", "-le", str(self.source)], text=True)
        self.assertIn("everyone allow read", listing)

    @unittest.skipUnless(sys.platform == "darwin", "native macOS ACL/xattr behavior")
    def test_macos_xattr_marker_cannot_hide_acl(self):
        subprocess.run(["chmod", "+a", "everyone allow read", str(self.source)], check=True)
        subprocess.run(["/usr/bin/xattr", "-w", "com.example.erddap-test", "keep", str(self.source)], check=True)
        status, output = self.run_cli("scan", "--datasets", self.source,
                                     "--old", "Lake Ontario", "--new", "Lake of America", "--plan", self.plan)
        self.assertEqual(status, 1, output)
        self.assertIn("extended attributes", output)
        self.assertFalse(self.plan.exists())
        self.assertEqual(self.source.read_bytes(), XML)
        listing = subprocess.check_output(["/bin/ls", "-le", str(self.source)], text=True)
        self.assertIn("everyone allow read", listing)

    def test_legacy_entry_points_offer_only_reviewed_commands(self):
        repository = Path(admin.__file__).parent
        for script in ("erddap_admin.py", "erddap_ssh_rename.sh"):
            command = ["bash"] if script.endswith(".sh") else [sys.executable]
            result = subprocess.run(command + [str(repository / script), "--help"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("rollback", result.stdout)
            self.assertIn("scan", result.stdout)


if __name__ == "__main__":
    unittest.main()
