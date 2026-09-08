#!/usr/bin/env python3
"""Reviewable ERDDAP metadata renames. Run locally after logging in via SSH."""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid

from erddap_metadata import PlanError, apply_edits, build_plan


class AdminError(Exception):
    """An operation was refused or could not be completed."""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def regular(path):
    """Reject aliases that could change which configuration we replace."""
    path = Path(os.path.abspath(path))
    for part in (path,) + tuple(path.parents):
        if part.is_symlink():
            raise AdminError("Symlink path is not supported; use the real path: %s" % path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise AdminError("Expected a regular file with one hard link: %s" % path)
    return path


def check_native_acl(path):
    # Darwin ACLs are not xattrs; some Darwin Python builds also lack xattr
    # APIs. Refuse both instead of silently discarding file metadata. The @
    # marker masks + when both are present. Linux access ACLs are copied
    # through their system.posix_acl_access xattr by stage_file.
    if sys.platform == "darwin":
        result = subprocess.run(["/bin/ls", "-lde", str(path)],
                                capture_output=True, text=True, env={"LC_ALL": "C"})
        if result.returncode or not result.stdout:
            raise AdminError("Cannot inspect native ACLs: %s" % path)
        if any(marker in result.stdout.split()[0] for marker in ("+", "@")):
            raise AdminError("Native macOS ACLs or extended attributes require a metadata-preserving editor; this utility refuses: %s" % path)


def read_regular(path):
    path = regular(path)
    check_native_acl(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise AdminError("Input changed while opening: %s" % path)
        data = stream.read()
        after = os.fstat(stream.fileno())
    if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise AdminError("Input changed while reading: %s" % path)
    return data, file_state(info)


def file_state(info):
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
            "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_directory(path):
    """Create private artifact directories, leaving existing permissions intact."""
    path = Path(path).absolute()
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700)
    if not path.is_dir() or path.is_symlink():
        raise AdminError("Expected an ordinary directory: %s" % path)
    return path.resolve()


def write_private(path, data):
    path = Path(path)
    private_directory(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        fsync_directory(path.parent)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def update_manifest(path, manifest):
    """Durably replace this transaction's journal without truncating it."""
    fd, name = tempfile.mkstemp(prefix=".manifest-", dir=Path(path).parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded(manifest))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fsync_directory(Path(path).parent)
    finally:
        Path(name).unlink(missing_ok=True)


def load_document(path, kind, confirm=None):
    data, _ = read_regular(path)
    digest = sha256(data)
    if confirm is not None and confirm != digest:
        raise AdminError("Confirmation must equal the current %s SHA256: %s" % (kind, digest))
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("kind") != kind or value.get("version") != 1:
        raise AdminError("Unsupported %s document" % kind)
    return value, digest


def plan_report(plan, digest=None):
    metadata = plan["metadata"]
    lines = ["ERDDAP METADATA RENAME PLAN", "Old phrase: %r" % metadata["old"],
             "New phrase: %r" % metadata["new"],
             "Matching: %s" % ("case-sensitive" if metadata["case_sensitive"] else "case-insensitive"),
             "Source files are unchanged by scan/review.", "", "Dataset inventory:"]
    for dataset in metadata["datasets"]:
        lines.append(json.dumps(dataset, ensure_ascii=False, sort_keys=True))
    lines += ["", "Input files (all are checked again before apply):"]
    for item in metadata["files"]:
        lines.append("%s | %s | %d edit(s)" % (item["path"], item["before_sha256"], len(item["edits"])))
    lines += ["", "Proposed field changes (%d):" % len(metadata["changes"])]
    for change in metadata["changes"]:
        lines.append(json.dumps(change, ensure_ascii=False, sort_keys=True))
    lines += ["", "Manual review / excluded matches (%d):" % len(metadata["manual_review"])]
    for entry in metadata["manual_review"]:
        lines.append(json.dumps(entry, ensure_ascii=False, sort_keys=True))
    audit = plan.get("public_audit")
    if audit is not None:
        lines += ["", "Public catalog audit (read-only; NOT additional apply operations):",
                  "Server: %s | datasets: %s | complete public scan: %s" % (
                      audit["server"], audit["dataset_count"], audit["complete"])]
        for item in audit["matches"]:
            lines.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
        for error in audit["errors"]:
            lines.append("AUDIT ERROR: " + json.dumps(error, ensure_ascii=False, sort_keys=True))
        lines.extend(audit["limitations"])
    lines += ["", "Affected top-level dataset IDs to reload: " + ", ".join(metadata["reload_ids"]),
              "Scope: existing eligible addAttributes only. Source-only metadata, data values,",
              "external charts, prebuilt FGDC/ISO files, IDs, URLs and source files need separate review.",
              "Do not hand-edit the plan. Change the scan options/input and create a new plan.",
              "Apply does not reload ERDDAP; use the explicit reload command after inspecting the result."]
    if digest:
        lines += ["", "Plan SHA256: " + digest,
                  "Apply requires --confirm followed by this full SHA256."]
    return "\n".join(lines) + "\n"


def scan(args):
    metadata = build_plan(args.datasets, args.old, args.new,
                          case_sensitive=args.case_sensitive, attributes=args.attribute)
    states = {}
    for item in metadata["files"]:
        data, state = read_regular(item["path"])
        if sha256(data) != item["before_sha256"]:
            raise AdminError("Input changed during scan: " + item["path"])
        states[item["path"]] = state
    plan = {"kind": "erddap-rename-plan", "version": 1,
            "metadata": metadata, "file_states": states}
    if args.server:
        from erddap_catalog import audit_catalog
        plan["public_audit"] = audit_catalog(args.server, args.old, args.new,
                                              case_sensitive=args.case_sensitive)
    data = encoded(plan)
    digest = sha256(data)
    report = plan_report(plan, digest)
    if args.plan:
        target = Path(args.plan).absolute()
        report_path = Path(str(target) + ".report.txt")
        sources = set(states)
        if str(target.resolve()) in sources or str(report_path.resolve()) in sources:
            raise AdminError("Plan/report output must not be an input XML file")
        if target.exists() or report_path.exists() or target.is_symlink() or report_path.is_symlink():
            raise AdminError("Plan/report already exists; choose new output paths")
        write_private(target, data)
        write_private(report_path, report.encode("utf-8"))
        print("Saved plan: %s\nSaved report: %s" % (target, report_path))
    print(report, end="")
    # A partial public audit is a visible failure, even though local planning succeeded.
    return 1 if plan.get("public_audit", {}).get("complete") is False else 0


@contextlib.contextmanager
def locked_files(paths):
    """Coordinate this utility's writers; administrators must pause other writers."""
    handles = []
    try:
        for value in sorted(set(paths)):
            path = regular(value)
            lock = path.with_name("." + path.name + ".erddap-rename.lock")
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            handles.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AdminError("Invalid lock file: %s" % lock)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AdminError("Another rename operation holds the lock: %s" % path)
        yield
    finally:
        for fd in reversed(handles):
            os.close(fd)
        # Keep lock files: unlinking a locked inode can allow concurrent writers.


def check_inputs(plan):
    original = plan["metadata"]
    current = build_plan(original["roots"], original["old"], original["new"],
                         case_sensitive=original["case_sensitive"], attributes=original["attributes"])
    if current != original:
        raise AdminError("Plan is stale or edited; scan again and review a new plan")
    for item in original["files"]:
        data, state = read_regular(item["path"])
        if sha256(data) != item["before_sha256"] or state != plan["file_states"].get(item["path"]):
            raise AdminError("Input content or file metadata changed; scan again: " + item["path"])


def stage_file(path, data):
    """Stage bytes beside their destination, preserving POSIX ownership/mode and xattrs."""
    path = regular(path)
    check_native_acl(path)
    info = path.stat()
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".rename-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            staged_info = os.fstat(stream.fileno())
            if (staged_info.st_uid, staged_info.st_gid) != (info.st_uid, info.st_gid):
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            if hasattr(os, "listxattr"):
                for key in os.listxattr(path):
                    value = os.getxattr(path, key)
                    os.setxattr(name, key, value)
                    if os.getxattr(name, key) != value:
                        raise AdminError("Could not preserve extended attribute: " + key)
            os.fsync(stream.fileno())
        return Path(name)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def replace_staged(staged, path):
    os.replace(staged, path)
    fsync_directory(Path(path).parent)


def transaction_dir(backup_root):
    root = private_directory(backup_root)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = root / (stamp + "-" + uuid.uuid4().hex[:12])
    directory.mkdir(mode=0o700)
    return directory


def apply_plan(args):
    plan, digest = load_document(args.plan, "erddap-rename-plan", args.confirm)
    files = plan["metadata"]["files"]
    changed = [item for item in files if item["edits"]]
    with locked_files([item["path"] for item in files]):
        check_inputs(plan)
        if not changed:
            print("No eligible metadata changes; no configuration files were modified.")
            return 0
        directory = transaction_dir(args.backup_dir)
        manifest_path = directory / "manifest.json"
        manifest = {"kind": "erddap-rename-manifest", "version": 1,
                    "plan_sha256": digest, "status": "preparing",
                    "reload_ids": plan["metadata"]["reload_ids"], "files": []}
        staged = {}
        installed = []
        try:
            for index, item in enumerate(changed):
                data, state = read_regular(item["path"])
                if sha256(data) != item["before_sha256"] or state != plan["file_states"][item["path"]]:
                    raise AdminError("Input changed while preparing: " + item["path"])
                updated = apply_edits(data, item["edits"])
                if sha256(updated) != item["after_sha256"]:
                    raise AdminError("Invalid planned output: " + item["path"])
                backup = directory / ("%04d.original" % index)
                write_private(backup, data)
                manifest["files"].append({"path": item["path"], "backup": str(backup),
                                          "before_sha256": item["before_sha256"],
                                          "after_sha256": item["after_sha256"],
                                          "original_state": state})
                staged[item["path"]] = stage_file(item["path"], updated)
            manifest["status"] = "prepared"
            update_manifest(manifest_path, manifest)
            # All inputs, including unchanged includes, are checked before the first replacement.
            check_inputs(plan)
            for item in changed:
                current, state = read_regular(item["path"])
                if sha256(current) != item["before_sha256"] or state != plan["file_states"][item["path"]]:
                    raise AdminError("Input changed before installation: " + item["path"])
                # Track before rename so an fsync failure still triggers restoration.
                installed.append(item["path"])
                replace_staged(staged[item["path"]], item["path"])
                staged.pop(item["path"])
            manifest["status"] = "applied"
            update_manifest(manifest_path, manifest)
        except BaseException as error:
            failures = []
            for item in reversed(manifest["files"]):
                if item["path"] not in installed:
                    continue
                try:
                    current, _ = read_regular(item["path"])
                    if sha256(current) == item["before_sha256"]:
                        continue
                    if sha256(current) != item["after_sha256"]:
                        raise AdminError("File changed after installation; not overwriting it")
                    data, _ = read_regular(item["backup"])
                    if sha256(data) != item["before_sha256"]:
                        raise AdminError("Backup checksum mismatch")
                    replacement = stage_file(item["path"], data)
                    try:
                        replace_staged(replacement, item["path"])
                    finally:
                        replacement.unlink(missing_ok=True)
                except BaseException as restore_error:
                    failures.append({"path": item["path"], "error": str(restore_error)})
            manifest["status"] = "recovery_required" if failures else "rolled_back_after_failure"
            manifest["error"] = str(error)
            manifest["recovery_errors"] = failures
            update_manifest(manifest_path, manifest)
            raise AdminError("Apply failed: %s. Status: %s. Inspect %s" % (
                error, manifest["status"], manifest_path)) from error
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)
    print("Applied %d file(s). ERDDAP has not been reloaded.\nManifest: %s\nManifest SHA256: %s" % (
        len(changed), manifest_path, sha256(manifest_path.read_bytes())))
    return 0


def check_manifest_files(manifest, expected_key):
    seen = set()
    for item in manifest["files"]:
        if item["path"] in seen:
            raise AdminError("Duplicate path in manifest")
        seen.add(item["path"])
        data, state = read_regular(item["path"])
        if sha256(data) != item[expected_key]:
            raise AdminError("File changed since transaction; refusing: " + item["path"])
        original = item["original_state"]
        if any(state[key] != original[key] for key in ("mode", "uid", "gid")):
            raise AdminError("File permissions/ownership changed; refusing: " + item["path"])


def rollback(args):
    manifest, digest = load_document(args.manifest, "erddap-rename-manifest", args.confirm)
    if manifest["status"] != "applied":
        raise AdminError("Rollback requires an applied transaction; current status: " + manifest["status"])
    staged = {}
    with locked_files([item["path"] for item in manifest["files"]]):
        # Prevent two waiting processes acting on an old manifest.
        load_document(args.manifest, "erddap-rename-manifest", digest)
        check_manifest_files(manifest, "after_sha256")
        try:
            for item in manifest["files"]:
                data, _ = read_regular(item["backup"])
                if sha256(data) != item["before_sha256"]:
                    raise AdminError("Backup checksum mismatch: " + item["backup"])
                staged[item["path"]] = stage_file(item["path"], data)
            check_manifest_files(manifest, "after_sha256")
            manifest["status"] = "rolling_back"
            update_manifest(args.manifest, manifest)
            for item in manifest["files"]:
                current, _ = read_regular(item["path"])
                if sha256(current) != item["after_sha256"]:
                    raise AdminError("File changed during rollback: " + item["path"])
                replace_staged(staged[item["path"]], item["path"])
                staged.pop(item["path"])
            manifest["status"] = "rolled_back"
            update_manifest(args.manifest, manifest)
        except BaseException as error:
            if manifest["status"] == "rolling_back":
                manifest["status"] = "recovery_required"
                manifest["error"] = str(error)
                update_manifest(args.manifest, manifest)
                raise AdminError("Rollback was interrupted; inspect backups and manifest: " + args.manifest) from error
            raise
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)
    print("Rollback complete. ERDDAP has not been reloaded.\nManifest: %s\nManifest SHA256: %s" % (
        args.manifest, sha256(Path(args.manifest).read_bytes())))
    return 0


def reload_datasets(args):
    manifest, digest = load_document(args.manifest, "erddap-rename-manifest", args.confirm)
    if manifest["status"] not in ("applied", "rolled_back", "rolled_back_after_failure"):
        raise AdminError("Reload requires a completed apply or rollback")
    ids = manifest["reload_ids"]
    if not isinstance(ids, list) or any(not isinstance(value, str) or
            not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value) or value in (".", "..") for value in ids):
        raise AdminError("Unsafe dataset ID in reload list")
    base = Path(args.big_parent).absolute()
    if base.is_symlink() or not base.is_dir():
        raise AdminError("--big-parent must identify the existing effective bigParentDirectory")
    flag_dir = base / "flag"
    if flag_dir.is_symlink() or not flag_dir.is_dir():
        raise AdminError("Expected ERDDAP's existing ordinary flag directory: %s" % flag_dir)
    with locked_files([item["path"] for item in manifest["files"]]):
        load_document(args.manifest, "erddap-rename-manifest", digest)
        check_manifest_files(manifest, "after_sha256" if manifest["status"] == "applied" else "before_sha256")
        for dataset_id in sorted(set(ids)):
            path = flag_dir / dataset_id
            try:
                write_private(path, b"")
            except FileExistsError:
                regular(path)
                print("Already queued: " + dataset_id)
            else:
                print("Queued normal reload: " + dataset_id)
    print("Reload requests queued. Check ERDDAP logs and served metadata; this is not proof of reload success.")
    return 0


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    commands = ap.add_subparsers(dest="command", required=True)
    scan_parser = commands.add_parser("scan", help="inventory XML and preview metadata changes; never edits source files")
    scan_parser.add_argument("--datasets", action="append", required=True, help="active datasets.xml path (repeatable)")
    scan_parser.add_argument("--old", required=True, help="literal phrase to replace")
    scan_parser.add_argument("--new", required=True, help="replacement wording supplied by your organization")
    scan_parser.add_argument("--case-sensitive", action="store_true")
    scan_parser.add_argument("--attribute", action="append", help="additional eligible textual attribute (repeatable)")
    scan_parser.add_argument("--plan", help="new plan JSON path; also writes PATH.report.txt")
    scan_parser.add_argument("--server", help="optional public ERDDAP audit URL (read-only; no automatic source overrides)")
    review_parser = commands.add_parser("review", help="show exact plan or backup manifest and its confirmation digest")
    review_group = review_parser.add_mutually_exclusive_group(required=True)
    review_group.add_argument("--plan")
    review_group.add_argument("--manifest")
    apply_parser = commands.add_parser("apply", help="apply an unchanged reviewed plan, backing up every changed file")
    apply_parser.add_argument("--plan", required=True)
    apply_parser.add_argument("--confirm", required=True, help="full SHA256 printed by review")
    apply_parser.add_argument("--backup-dir", required=True, help="parent directory for a unique private transaction backup")
    rollback_parser = commands.add_parser("rollback", help="restore verified original files from an applied transaction")
    rollback_parser.add_argument("--manifest", required=True)
    rollback_parser.add_argument("--confirm", required=True)
    reload_parser = commands.add_parser("reload", help="explicitly request normal dataset reloads after apply/rollback")
    reload_parser.add_argument("--manifest", required=True)
    reload_parser.add_argument("--confirm", required=True)
    reload_parser.add_argument("--big-parent", required=True, help="effective ERDDAP bigParentDirectory with existing flag/")
    return ap


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    # No mode or scan-style flags default to read-only scan; obsolete flags fail closed.
    if arguments and arguments[0].startswith("--") and arguments[0] != "--help":
        arguments.insert(0, "scan")
    args = parser().parse_args(arguments)
    try:
        if args.command == "scan":
            return scan(args)
        if args.command == "review":
            if args.plan:
                plan, digest = load_document(args.plan, "erddap-rename-plan")
                print(plan_report(plan, digest), end="")
            else:
                manifest, digest = load_document(args.manifest, "erddap-rename-manifest")
                print(encoded(manifest).decode("utf-8"), end="")
                print("Manifest SHA256: " + digest)
            return 0
        if args.command == "apply":
            return apply_plan(args)
        if args.command == "rollback":
            return rollback(args)
        if args.command == "reload":
            return reload_datasets(args)
    except (AdminError, PlanError, OSError, ValueError, KeyError, TypeError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
