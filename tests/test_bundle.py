import errno
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

from scripts import build_admin_bundle as bundle


XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<erddapDatasets>
  <dataset type="EDDGridFromNcFiles" datasetID="lake">
    <addAttributes><att name="title">Lake Ontario waves</att></addAttributes>
  </dataset>
</erddapDatasets>
'''


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.archive = self.root / "erddapctl.pyz"

    def tearDown(self):
        self.temp.cleanup()

    def run_bundle(self, *args):
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        return subprocess.run(
            [sys.executable, str(self.archive), *map(str, args)],
            cwd=self.root, env=environment, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30,
        )

    def run_bundle_terminal(self, *args, confirm=True):
        """Exercise genuine terminal checks and answer the displayed prompt."""
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        master, slave = pty.openpty()
        process = None
        output = bytearray()
        sent = False
        deadline = time.monotonic() + 10
        try:
            process = subprocess.Popen(
                [sys.executable, str(self.archive), *map(str, args)],
                cwd=self.root, env=environment, stdin=slave, stdout=slave,
                stderr=slave, close_fds=True,
            )
            os.close(slave)
            slave = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.fail("Bundled terminal command timed out:\n" + output.decode(errors="replace"))
                readable, _, _ = select.select([master], [], [], min(remaining, 0.1))
                if readable:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        # Linux PTYs report EIO when the child closes the slave.
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                    match = re.search(rb"type ([A-Z]+ [0-9a-f]{12})\. Press Enter to cancel\.\r?\n> ", output)
                    if match and not sent:
                        self.assertEqual(match.group(1).split()[0].decode(), args[0].upper())
                        os.write(master, (match.group(1) if confirm else b"") + b"\n")
                        sent = True
                elif process.poll() is not None:
                    break
            status = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            text = output.decode(errors="replace")
            self.assertTrue(sent, "No confirmation prompt appeared:\n" + text)
            return status, text
        finally:
            if slave is not None:
                os.close(slave)
            os.close(master)
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=1)

    def test_archive_is_deterministic_and_contains_only_distribution_files(self):
        digest = bundle.build_bundle(self.archive)
        second = self.root / "second.pyz"
        bundle.build_bundle(second)
        self.assertEqual(self.archive.read_bytes(), second.read_bytes())
        self.assertEqual(digest, hashlib.sha256(self.archive.read_bytes()).hexdigest())
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(set(archive.namelist()), {
                "__main__.py", *bundle.RUNTIME_FILES, *bundle.DOCUMENT_FILES,
            })
            self.assertIsNone(archive.testzip())

    def test_existing_output_is_never_overwritten(self):
        self.archive.write_bytes(b"earlier administrator distribution")
        with self.assertRaises(FileExistsError):
            bundle.build_bundle(self.archive)
        self.assertEqual(self.archive.read_bytes(), b"earlier administrator distribution")

    def test_missing_source_leaves_no_output(self):
        with self.assertRaisesRegex(ValueError, "Required bundle source"):
            bundle.build_bundle(self.archive, source_root=self.root)
        self.assertFalse(self.archive.exists())

    def test_portable_cli_setup_doctor_scan_and_history_without_repository(self):
        bundle.build_bundle(self.archive)
        source = self.root / "datasets.xml"
        source.write_bytes(XML)
        profile = self.root / "site.json"
        workspace = self.root / "changes"

        result = self.run_bundle("--help")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("setup", result.stdout)
        self.assertIn("rename", result.stdout)

        result = self.run_bundle("setup", "--profile", profile, "--datasets", source,
                                 "--workspace", workspace)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(profile.is_file())

        result = self.run_bundle("doctor", "--profile", profile)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        result = self.run_bundle("rename", "--profile", profile, "--old", "Lake Ontario",
                                 "--new", "Lake of America", "--scan-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(source.read_bytes(), XML)
        plans = list(workspace.glob("*/plan.json"))
        self.assertEqual(len(plans), 1)
        self.assertIn(b"Lake of America", plans[0].read_bytes())

        result = self.run_bundle("history", "--profile", profile)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(plans[0].parent.name, result.stdout)

    def test_portable_terminal_cancel_apply_and_rollback(self):
        bundle.build_bundle(self.archive)
        source = self.root / "datasets.xml"
        source.write_bytes(XML)
        profile = self.root / "site.json"
        workspace = self.root / "changes"
        big_parent = self.root / "erddapData"
        flags = big_parent / "flag"
        flags.mkdir(parents=True)
        result = self.run_bundle("setup", "--profile", profile, "--datasets", source,
                                 "--workspace", workspace, "--big-parent", big_parent)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.run_bundle("rename", "--profile", profile, "--old", "Lake Ontario",
                                 "--new", "Lake of America", "--scan-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plans = list(workspace.glob("*/plan.json"))
        self.assertEqual(len(plans), 1)
        job = plans[0].parent

        status, output = self.run_bundle_terminal("apply", "--job", job, confirm=False)
        self.assertEqual(status, 0, output)
        self.assertIn("Cancelled", output)
        self.assertEqual(source.read_bytes(), XML)
        self.assertFalse((job / "backups").exists())

        status, output = self.run_bundle_terminal("apply", "--job", job)
        self.assertEqual(status, 0, output)
        self.assertEqual(source.read_bytes(), XML.replace(b"Lake Ontario", b"Lake of America"))
        self.assertEqual(list(flags.iterdir()), [])
        manifests = list((job / "backups").glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(json.loads(manifests[0].read_text())["status"], "applied")

        status, output = self.run_bundle_terminal("rollback", "--job", job)
        self.assertEqual(status, 0, output)
        self.assertEqual(source.read_bytes(), XML)
        self.assertEqual(list(flags.iterdir()), [])
        self.assertEqual(json.loads(manifests[0].read_text())["status"], "rolled_back")


if __name__ == "__main__":
    unittest.main()
