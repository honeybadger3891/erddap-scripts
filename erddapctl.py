#!/usr/bin/env python3
"""Guided ERDDAP administration with reusable site settings and change history."""
import argparse
import contextlib
import datetime
import io
import json
from pathlib import Path
import sys
import uuid

if sys.version_info < (3, 9) or sys.platform not in ("linux", "darwin"):
    raise SystemExit("ERDDAP administrator utility requires Python 3.9+ on Linux or macOS.")

import erddap_admin_rename as core
from erddap_site import create_profile, doctor, load_profile, validate_profile


JOB_KIND = "erddap-admin-job"
VERSION = "1.1.0"


def require_terminal():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise core.AdminError("Interactive review requires a terminal. Use rename --scan-only for a saved plan, "
                              "or the advanced command with an explicitly reviewed digest for automation.")


def ask(label, default=""):
    suffix = " [%s]" % default if default else ""
    return input(label + suffix + ": ").strip() or default


def confirm(action, digest):
    require_terminal()
    token = action.upper() + " " + digest[:12]
    print("\nTo confirm this reviewed %s, type %s. Press Enter to cancel." % (action, token))
    return input("> ").strip() == token


def checked_job(directory):
    directory = Path(directory).absolute()
    data, _ = core.read_regular(directory / "job.json")
    job = json.loads(data)
    if not isinstance(job, dict) or job.get("kind") != JOB_KIND or job.get("version") != 1:
        raise core.AdminError("Not an ERDDAP change directory: %s" % directory)
    validate_profile(job["site"])
    if job.get("id") != directory.name or not isinstance(job.get("created_at"), str):
        raise core.AdminError("Invalid change record: %s" % directory)
    if not isinstance(job.get("old"), str) or not isinstance(job.get("new"), str):
        raise core.AdminError("Change record is missing the requested names")
    return directory, job, core.sha256(data)


def manifest_for(directory):
    paths = sorted((directory / "backups").glob("*/manifest.json"))
    if len(paths) > 1:
        raise core.AdminError("Multiple transaction manifests need individual review; use advanced review --manifest PATH: "
                              + ", ".join(map(str, paths)))
    if not paths:
        return None, None, None
    manifest, digest = core.load_document(paths[0], "erddap-rename-manifest")
    return paths[0], manifest, digest


def job_status(directory, job):
    _, manifest, _ = manifest_for(directory)
    if manifest is not None:
        return manifest["status"]
    if job.get("scan_status") != "ready":
        return job.get("scan_status", "scan_interrupted")
    plan, _ = core.load_document(directory / "plan.json", "erddap-rename-plan")
    return "ready" if plan["metadata"]["changes"] else "no_changes"


def require_same_job(directory, digest):
    _, _, current = checked_job(directory)
    if current != digest:
        raise core.AdminError("The saved change settings changed during review; review the change again")


def plan_for_job(directory, job):
    plan, digest = core.load_document(directory / "plan.json", "erddap-rename-plan")
    metadata = plan["metadata"]
    if metadata["old"] != job["old"] or metadata["new"] != job["new"]:
        raise core.AdminError("The saved names do not match this plan; rescan into a new change directory")
    if set(metadata["roots"]) != set(job["site"]["datasets"]):
        raise core.AdminError("The plan's inputs differ from the saved site settings")
    return plan, digest


def readable_plan(plan, digest):
    metadata = plan["metadata"]
    print("\nRename %r to %r" % (metadata["old"], metadata["new"]))
    changed_files = sum(bool(item["edits"]) for item in metadata["files"])
    print("%d datasets inspected | %d fields proposed | %d files to change | %d manual findings" % (
        len(metadata["datasets"]), len(metadata["changes"]), changed_files, len(metadata["manual_review"])))
    for number, change in enumerate(metadata["changes"], 1):
        print("\n%d. Dataset %r / %s / %s" % (
            number, change["dataset_id"], repr(change.get("variable")) if change.get("variable") else "global metadata",
            change["attribute"]))
        print("   File: %s" % change["path"])
        print("   Before: %s" % json.dumps(change["before"], ensure_ascii=False))
        print("   After:  %s" % json.dumps(change["after"], ensure_ascii=False))
    if metadata["manual_review"]:
        print("\nManual review (these are not additional automatic changes):")
        for finding in metadata["manual_review"]:
            print(json.dumps(finding, ensure_ascii=False, sort_keys=True))
    audit = plan.get("public_audit")
    if audit is not None:
        print("\nPublic audit: %d datasets; %d candidates; complete=%s" % (
            audit["dataset_count"], len(audit["matches"]), audit["complete"]))
        for candidate in audit["matches"]:
            print(json.dumps(candidate, ensure_ascii=False, sort_keys=True))
        for error in audit["errors"]:
            print("AUDIT ERROR: " + json.dumps(error, ensure_ascii=False))
    print("\nInputs checked again before apply:")
    for item in metadata["files"]:
        print("  %s" % item["path"])
    print("Reload IDs: " + (", ".join(metadata["reload_ids"]) or "(none)"))
    print("Full plan SHA256: " + digest)
    print("Apply updates the listed configuration values. Reload is a separate action.")


def print_doctor(profile, compact=False):
    findings = doctor(profile)
    for item in findings:
        if compact and (item["status"] == "ok" or item["name"] in ("server", "reload flags", "limits") and item["status"] != "error"):
            continue
        print("[%s] %s: %s" % (item["status"].upper(), item["name"], item["detail"]))
    failed = any(item["status"] == "error" for item in findings)
    if compact and not failed:
        print("Setup checks passed. Run doctor for the full path and permission report.")
    return 1 if failed else 0


def setup(args):
    datasets, workspace = args.datasets, args.workspace
    big_parent, server = args.big_parent, args.server
    if not datasets or not workspace:
        require_terminal()
        if not datasets:
            datasets = [ask("Active datasets.xml path")]
        if not workspace:
            workspace = ask("Private directory for change history and backups")
        if big_parent is None:
            big_parent = ask("Effective bigParentDirectory (optional; needed for reload)") or None
        if server is None:
            server = ask("Public ERDDAP URL (optional)") or None
    profile = create_profile(args.profile, datasets, workspace, big_parent=big_parent, server=server)
    print("Saved site profile: %s" % Path(args.profile).absolute())
    print("Site settings are saved once; each change retains its own snapshot.")
    return print_doctor(profile)


def rename(args):
    if not args.scan_only:
        require_terminal()
    profile = load_profile(args.profile)
    if args.audit_public and not profile.get("server"):
        raise core.AdminError("Save a public server URL in the site profile before selecting --audit-public")
    old, new = args.old, args.new
    if not old or not new:
        require_terminal()
        old = old or ask("Current name")
        new = new or ask("Replacement name")
    # Validate names before creating a job or making any HTTP requests.
    from erddap_metadata import replace_phrase
    replace_phrase("", old, new, case_sensitive=args.case_sensitive)
    print("Checking saved site settings...")
    if print_doctor(profile, compact=True):
        raise core.AdminError("Resolve the failed setup checks before starting a managed change")
    workspace = core.private_directory(profile["workspace"])
    now = datetime.datetime.now(datetime.timezone.utc)
    job_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    directory = workspace / job_id
    directory.mkdir(mode=0o700)
    job = {"kind": JOB_KIND, "version": 1, "id": job_id,
           "created_at": now.isoformat(), "site": profile, "old": old, "new": new,
           "scan_status": "scan_interrupted"}
    core.write_private(directory / "job.json", core.encoded(job))
    print("\nChange directory: %s" % directory)
    print("Scanning configuration%s..." % (" and public metadata" if args.audit_public else ""))
    scan_output = io.StringIO()
    with contextlib.redirect_stdout(scan_output), contextlib.redirect_stderr(scan_output):
        status = core.main(["scan", *sum((["--datasets", path] for path in profile["datasets"]), []),
                            "--old", old, "--new", new, "--plan", str(directory / "plan.json"),
                            *(["--case-sensitive"] if args.case_sensitive else []),
                            *sum((["--attribute", value] for value in args.attribute or []), []),
                            *(["--server", profile["server"]] if args.audit_public else [])])
    core.write_private(directory / "scan.log", scan_output.getvalue().encode("utf-8"))
    job["scan_status"] = "ready" if status == 0 else "scan_failed"
    if status and (directory / "plan.json").exists():
        saved_plan, _ = core.load_document(directory / "plan.json", "erddap-rename-plan")
        if saved_plan.get("public_audit", {}).get("complete") is False:
            job["scan_status"] = "audit_incomplete"
    core.update_manifest(directory / "job.json", job)
    if status:
        print(scan_output.getvalue(), end="")
        print("The scan did not complete. Its saved evidence remains in %s; create a new scan after resolving it." % directory)
        return status
    print("\nSaved report: %s" % (directory / "plan.json.report.txt"))
    if args.scan_only:
        print("Scan only: configuration is unchanged. Use review/apply --job with this change directory.")
        return 0
    return apply_job(directory)


def review_job(directory):
    directory, job, _ = checked_job(directory)
    print("Change: %s\nCreated: %s\nStatus: %s" % (directory, job["created_at"], job_status(directory, job)))
    if (directory / "plan.json").exists():
        plan, digest = plan_for_job(directory, job)
        readable_plan(plan, digest)
    path, manifest, digest = manifest_for(directory)
    if manifest is not None:
        print("\nTransaction: %s\nStatus: %s\nManifest SHA256: %s" % (path, manifest["status"], digest))
        for item in manifest["files"]:
            print("  %s\n    Backup: %s" % (item["path"], item["backup"]))
    return 0


def apply_job(directory):
    require_terminal()
    directory, job, job_digest = checked_job(directory)
    status = job_status(directory, job)
    if status == "no_changes":
        print("No eligible changes. Review any manual findings in %s." % (directory / "plan.json.report.txt"))
        return 0
    if status != "ready":
        raise core.AdminError("This change cannot be applied: %s. Review its history or create a fresh scan." % status)
    plan, digest = plan_for_job(directory, job)
    readable_plan(plan, digest)
    if not confirm("apply", digest):
        print("Cancelled. Configuration is unchanged; the saved plan is available for later review.")
        return 0
    require_same_job(directory, job_digest)
    result = core.main(["apply", "--plan", str(directory / "plan.json"), "--confirm", digest,
                        "--backup-dir", str(directory / "backups")])
    if result == 0:
        print("\nApply finished. Use reload --job %s when ready to request dataset reloads." % directory)
    return result


def change_transaction(directory, action):
    require_terminal()
    directory, job, job_digest = checked_job(directory)
    path, manifest, digest = manifest_for(directory)
    if manifest is None:
        raise core.AdminError("This change has no transaction; apply a reviewed plan first")
    plan, plan_digest = plan_for_job(directory, job)
    expected = {item["path"]: (item["before_sha256"], item["after_sha256"])
                for item in plan["metadata"]["files"] if item["edits"]}
    actual = {item["path"]: (item["before_sha256"], item["after_sha256"]) for item in manifest["files"]}
    if (manifest.get("plan_sha256") != plan_digest or expected != actual
            or manifest["reload_ids"] != plan["metadata"]["reload_ids"]):
        raise core.AdminError("Transaction does not belong to this saved plan; review the change records")
    allowed = {"applied"} if action == "rollback" else {"applied", "rolled_back", "rolled_back_after_failure"}
    if manifest["status"] not in allowed:
        raise core.AdminError("%s is unavailable for transaction status %s; review the manifest" % (action, manifest["status"]))
    print("\nChange: %s\nTransaction: %s\nStatus: %s" % (directory, path, manifest["status"]))
    if action == "reload":
        base = job["site"].get("big_parent")
        if not base:
            raise core.AdminError("This change has no saved bigParentDirectory. Use advanced reload with an explicitly reviewed path.")
        print("Flag directory: %s" % (Path(base) / "flag"))
        print("Dataset IDs: " + (", ".join(manifest["reload_ids"]) or "(none)"))
        print("Reload requests are asynchronous; verify ERDDAP logs and served metadata afterward.")
    else:
        print("Restore these original files:")
        for item in manifest["files"]:
            print("  %s\n    from %s" % (item["path"], item["backup"]))
        print("Rollback restores configuration. Reload remains a separate action.")
    print("Manifest SHA256: " + digest)
    if not confirm(action, digest):
        print("Cancelled. No %s was requested." % action)
        return 0
    require_same_job(directory, job_digest)
    command = [action, "--manifest", str(path), "--confirm", digest]
    if action == "reload":
        command += ["--big-parent", base]
    return core.main(command)


def history(profile):
    workspace = Path(profile["workspace"])
    if not workspace.exists():
        print("No changes recorded yet. Workspace: %s" % workspace)
        return 0
    print("Change history: %s\nStatuses come from saved plans and transaction journals." % workspace)
    paths = sorted(workspace.glob("*/job.json"), reverse=True)
    for number, path in enumerate(paths, 1):
        try:
            directory, job, _ = checked_job(path.parent)
            print("%d. %s | %s | %r -> %r" % (number, directory.name, job_status(directory, job), job["old"], job["new"]))
        except (core.AdminError, OSError, ValueError, KeyError, TypeError) as error:
            print("%d. %s | NEEDS REVIEW | %s" % (number, path.parent.name, error))
    if not paths:
        print("No changes recorded yet.")
    return 0


def menu(profile_path):
    require_terminal()
    while True:
        profile = load_profile(profile_path)
        print("\nERDDAP administrator\nSite inputs: %s\n1. Scan a name change\n2. Show change history\n3. Open a saved change\n4. Check setup\n0. Exit" % ", ".join(profile["datasets"]))
        choice = ask("Select", "0")
        if choice == "0":
            return 0
        if choice == "1":
            rename(argparse.Namespace(profile=profile_path, old=None, new=None, scan_only=False,
                                      audit_public=False, case_sensitive=False, attribute=None))
        elif choice == "2":
            history(profile)
        elif choice == "3":
            history(profile)
            paths = sorted(Path(profile["workspace"]).glob("*/job.json"), reverse=True)
            selected = ask("Change number or ID from history (Enter to return)")
            if not selected:
                continue
            if selected.isdigit():
                if not 1 <= int(selected) <= len(paths):
                    print("Choose one of the displayed change numbers.")
                    continue
                selected = paths[int(selected) - 1].parent.name
            if selected in (".", "..") or Path(selected).name != selected:
                raise core.AdminError("Select a single change ID from the displayed history")
            directory = Path(profile["workspace"]) / selected
            review_job(directory)
            action = ask("Action: apply, reload, rollback, or Enter to return")
            if action == "apply":
                apply_job(directory)
            elif action in ("reload", "rollback"):
                change_transaction(directory, action)
        elif choice == "4":
            print_doctor(profile)


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", action="version", version="ERDDAP administrator " + VERSION)
    commands = ap.add_subparsers(dest="command", required=True)
    setup_parser = commands.add_parser("setup", help="save this installation's paths once; prompts for missing required values")
    setup_parser.add_argument("--profile", required=True)
    setup_parser.add_argument("--datasets", action="append")
    setup_parser.add_argument("--workspace", help="private directory for all change plans/backups")
    setup_parser.add_argument("--big-parent", help="effective ERDDAP bigParentDirectory")
    setup_parser.add_argument("--server", help="optional public ERDDAP URL")
    for name, help_text in (("doctor", "check setup without changing files or contacting ERDDAP"),
                            ("history", "list saved changes and transaction states"),
                            ("menu", "interactive terminal menu for everyday administration")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--profile", required=True)
    sub = commands.add_parser("rename", help="scan, review, and optionally confirm a metadata rename")
    sub.add_argument("--profile", required=True)
    sub.add_argument("--old")
    sub.add_argument("--new")
    sub.add_argument("--scan-only", action="store_true", help="save a plan without prompting or applying")
    sub.add_argument("--audit-public", action="store_true", help="also inspect all public metadata (may take several minutes)")
    sub.add_argument("--case-sensitive", action="store_true")
    sub.add_argument("--attribute", action="append")
    for name in ("review", "apply", "reload", "rollback"):
        sub = commands.add_parser(name, help="%s a saved change directory" % name)
        sub.add_argument("--job", required=True)
    advanced = commands.add_parser("advanced", help="pass arguments to the original digest-confirmed administration CLI")
    advanced.add_argument("arguments", nargs=argparse.REMAINDER)
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "setup":
            return setup(args)
        if args.command == "doctor":
            return print_doctor(load_profile(args.profile))
        if args.command == "history":
            return history(load_profile(args.profile))
        if args.command == "menu":
            return menu(args.profile)
        if args.command == "rename":
            return rename(args)
        if args.command == "review":
            return review_job(args.job)
        if args.command == "apply":
            return apply_job(args.job)
        if args.command in ("reload", "rollback"):
            return change_transaction(args.job, args.command)
        if args.command == "advanced":
            return core.main(args.arguments)
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. Review saved change history before continuing any interrupted operation.", file=sys.stderr)
        return 130
    except (core.AdminError, core.PlanError, OSError, ValueError, KeyError, TypeError) as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
