import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import erddap_admin_rename as admin
import erddap_site as site


XML = b'''<erddapDatasets><dataset type="EDDGridFromNcFiles" datasetID="lake"><addAttributes><att name="title">Lake Ontario waves</att></addAttributes></dataset></erddapDatasets>'''


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "datasets.xml"
        self.source.write_bytes(XML)
        self.profile_path = self.root / "site.json"
        self.workspace = self.root / "operations"
        self.profile = {"kind": site.PROFILE_KIND, "version": 1,
                        "datasets": [str(self.source)], "workspace": str(self.workspace)}

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.lstat().st_mode, p.lstat().st_mtime_ns,
                                               p.read_bytes() if p.is_file() else None)
                for p in self.root.rglob("*")}

    def check_named(self, checks, name):
        return next(c for c in checks if c["name"] == name)

    def test_create_roundtrip_is_private_and_workspace_remains_absent(self):
        created = site.create_profile(self.profile_path, [str(self.source)], self.workspace,
                                      server="https://example.org/erddap/")
        self.assertEqual(site.load_profile(self.profile_path), created)
        self.assertEqual(created["server"], "https://example.org/erddap")
        self.assertEqual(stat.S_IMODE(self.profile_path.stat().st_mode), 0o600)
        self.assertFalse(self.workspace.exists())
        self.assertEqual(self.source.read_bytes(), XML)

    def test_no_overwrite_or_profile_source_collision(self):
        self.profile_path.write_text("original")
        with self.assertRaisesRegex(admin.AdminError, "already exists"):
            site.create_profile(self.profile_path, [str(self.source)], self.workspace)
        self.assertEqual(self.profile_path.read_text(), "original")
        missing = self.root / "missing.xml"
        with self.assertRaisesRegex(admin.AdminError, "separate"):
            site.create_profile(missing, [str(missing)], self.workspace)
        self.assertFalse(missing.exists())
        self.assertFalse(self.workspace.exists())

    def test_relative_explicit_inputs_are_saved_as_absolute(self):
        with mock.patch.object(Path, "cwd", return_value=self.root), mock.patch("os.getcwd", return_value=str(self.root)):
            created = site.create_profile("site.json", ["datasets.xml"], "operations")
        self.assertEqual(created["datasets"], [str(self.source)])
        self.assertEqual(created["workspace"], str(self.workspace))

    def test_profile_can_create_its_explicitly_requested_parent_workspace(self):
        path = self.workspace / "site.json"
        created = site.create_profile(path, [str(self.source)], self.workspace)
        self.assertEqual(site.load_profile(path), created)
        self.assertEqual(stat.S_IMODE(self.workspace.stat().st_mode), 0o700)
        self.assertEqual(list(self.workspace.iterdir()), [path])

    def test_rejects_unknown_fields_types_duplicates_and_credentials(self):
        variants = [{"password": "not-supported"}, {"version": True}, {"version": 2},
                    {"kind": "other"}, {"datasets": []}, {"datasets": str(self.source)},
                    {"datasets": [str(self.source), str(self.source)]}, {"datasets": [7]},
                    {"workspace": "relative"}, {"workspace": None}, {"big_parent": None},
                    {"workspace": "//ambiguous/workspace"},
                    {"server": "https://user:secret@example.org/erddap"},
                    {"server": "https://example.org/erddap?token=secret"}]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(admin.AdminError):
                site.validate_profile(dict(self.profile, **changes))
        with self.assertRaises(admin.AdminError):
            site.validate_profile({"kind": site.PROFILE_KIND})

    def test_symlinks_dotdot_and_hardlinks_cannot_be_normalized_away(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        candidates = [str(alias / "datasets.xml"), str(alias / ".." / self.root.name / "datasets.xml"),
                      str(self.root) + "/../" + self.root.name + "/datasets.xml"]
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(admin.AdminError):
                site.create_profile(self.profile_path, [candidate], self.workspace)
        with self.assertRaises(admin.AdminError):
            site.create_profile(alias / "site.json", [str(self.source)], self.workspace)
        with self.assertRaises(admin.AdminError):
            site.validate_profile(dict(self.profile, workspace=str(alias / "missing")))
        second = self.root / "hardlink.xml"
        os.link(self.source, second)
        with self.assertRaisesRegex(admin.AdminError, "hard link"):
            site.validate_profile(self.profile)
        self.assertFalse(self.profile_path.exists())

    def test_load_rejects_invalid_json_or_edited_profile(self):
        self.profile_path.write_text("{")
        with self.assertRaises(json.JSONDecodeError):
            site.load_profile(self.profile_path)
        self.profile_path.write_bytes(admin.encoded(dict(self.profile, command="anything")))
        with self.assertRaisesRegex(admin.AdminError, "unknown"):
            site.load_profile(self.profile_path)

    def test_doctor_reads_includes_without_any_writes_or_network(self):
        included = self.root / "lake.xml"
        included.write_bytes(XML.split(b"<erddapDatasets>")[1].split(b"</erddapDatasets>")[0])
        self.source.write_text('<erddapDatasets xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include href="lake.xml"/></erddapDatasets>')
        big = self.root / "big"
        (big / "flag").mkdir(parents=True)
        profile = dict(self.profile, big_parent=str(big), server="https://example.org/erddap")
        before = self.snapshot()
        with mock.patch.object(admin, "write_private", side_effect=AssertionError("write attempted")), \
             mock.patch.object(admin, "private_directory", side_effect=AssertionError("directory creation attempted")), \
             mock.patch("os.mkdir", side_effect=AssertionError("mkdir attempted")), \
             mock.patch("tempfile.mkstemp", side_effect=AssertionError("staging attempted")), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("network attempted")):
            checks = site.doctor(profile)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(any(c["status"] == "error" for c in checks), checks)
        self.assertEqual(self.check_named(checks, "source: " + str(included))["status"], "ok")
        self.assertEqual(self.check_named(checks, "reload flags")["status"], "ok")
        self.assertIn(str(big / "flag"), self.check_named(checks, "reload flags")["detail"])
        self.assertIn("generated", self.check_named(checks, "limits")["detail"])
        self.assertFalse(self.workspace.exists())

    def test_missing_sources_and_reload_flags_are_actionable_errors(self):
        self.source.unlink()
        checks = site.doctor(dict(self.profile, big_parent=str(self.root / "missing-big")))
        self.assertEqual(self.check_named(checks, "source graph")["status"], "error")
        self.assertEqual(self.check_named(checks, "source: " + str(self.source))["status"], "error")
        self.assertEqual(self.check_named(checks, "reload flags")["status"], "error")
        self.assertIn("flag", self.check_named(checks, "reload flags")["detail"])
        checks = site.doctor(self.profile)
        self.assertEqual(self.check_named(checks, "reload flags")["status"], "warning")

    def test_invalid_profile_and_flag_symlink_are_errors(self):
        checks = site.doctor(dict(self.profile, workspace="../somewhere"))
        self.assertEqual(self.check_named(checks, "profile")["status"], "error")
        big = self.root / "big"
        big.mkdir()
        (big / "flag").symlink_to(self.root, target_is_directory=True)
        checks = site.doctor(dict(self.profile, big_parent=str(big)))
        self.assertEqual(self.check_named(checks, "reload flags")["status"], "error")
        self.assertIn("symlink", self.check_named(checks, "reload flags")["detail"])

    def test_existing_workspace_permissions_are_reported_and_not_changed(self):
        self.workspace.mkdir(mode=0o755)
        self.workspace.chmod(0o755)
        checks = site.doctor(self.profile)
        self.assertEqual(self.check_named(checks, "workspace")["status"], "error")
        self.assertEqual(stat.S_IMODE(self.workspace.stat().st_mode), 0o755)
        self.workspace.chmod(0o700)
        self.assertEqual(self.check_named(site.doctor(self.profile), "workspace")["status"], "ok")

    def test_read_regular_acl_failure_is_reported_for_includes(self):
        original = admin.read_regular
        def denied(path):
            if str(path) == str(self.source):
                raise admin.AdminError("unsupported native ACL")
            return original(path)
        with mock.patch.object(admin, "read_regular", side_effect=denied):
            checks = site.doctor(self.profile)
        result = self.check_named(checks, "source: " + str(self.source))
        self.assertEqual(result["status"], "error")
        self.assertIn("ACL", result["detail"])

    def test_directory_access_failure_and_owner_warning(self):
        with mock.patch.object(site, "_accessible", return_value=False):
            checks = site.doctor(self.profile)
        self.assertEqual(self.check_named(checks, "source directory: " + str(self.root))["status"], "error")
        self.assertEqual(self.check_named(checks, "workspace parent")["status"], "error")
        with mock.patch("os.geteuid", return_value=self.source.stat().st_uid + 1000):
            checks = site.doctor(self.profile)
        self.assertEqual(self.check_named(checks, "ownership: " + str(self.source))["status"], "warning")


if __name__ == "__main__":
    unittest.main()
