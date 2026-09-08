"""Saved explicit site paths and a read-only preflight for SSH administrators.

A profile is configuration for this utility, never ERDDAP configuration. It
contains no credentials and cannot select commands or executable code.
"""

import os
from pathlib import Path
import stat
import sys

import erddap_admin_rename as admin
from erddap_catalog import normalize_server
from erddap_metadata import PlanError, build_plan


PROFILE_KIND = "erddap-site-profile"
PROFILE_VERSION = 1
_REQUIRED = {"kind", "version", "datasets", "workspace"}
_OPTIONAL = {"big_parent", "server"}


def _path(value, label, *, absolute=False):
    """Check the supplied spelling before normalization can conceal an alias."""
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise admin.AdminError("%s must be a nonempty path without control characters" % label)
    if ".." in value.split(os.sep):
        raise admin.AdminError("%s must not contain '..'; supply its explicit real path" % label)
    if value.startswith(os.sep * 2):
        raise admin.AdminError("%s must not use an ambiguous double-slash root" % label)
    if absolute and (not os.path.isabs(value) or os.path.abspath(value) != value):
        raise admin.AdminError("%s must use an absolute, normalized real path" % label)
    spelled = Path(value) if os.path.isabs(value) else Path.cwd() / value
    for component in (*reversed(spelled.parents), spelled):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise admin.AdminError("Cannot inspect %s: %s" % (component, exc)) from exc
        if stat.S_ISLNK(info.st_mode):
            raise admin.AdminError("%s contains a symlink: %s; supply its real path" % (label, component))
    return os.path.abspath(value)


def validate_profile(profile):
    """Validate a stored profile without creating directories or contacting ERDDAP.

    Missing files are allowed here so that doctor can explain mount, path, and
    permissions problems. Existing symlink aliases are always refused.
    """
    if not isinstance(profile, dict):
        raise admin.AdminError("Site profile must be a JSON object")
    if set(profile) - _REQUIRED - _OPTIONAL or not _REQUIRED.issubset(profile):
        raise admin.AdminError("Site profile has missing or unknown keys; allowed keys: %s" %
                               ", ".join(sorted(_REQUIRED | _OPTIONAL)))
    if profile["kind"] != PROFILE_KIND or type(profile["version"]) is not int or profile["version"] != PROFILE_VERSION:
        raise admin.AdminError("Unsupported site profile kind or version")
    datasets = profile["datasets"]
    if not isinstance(datasets, list) or not datasets:
        raise admin.AdminError("Site profile datasets must be a nonempty list of paths")
    roots = [_path(value, "datasets path", absolute=True) for value in datasets]
    if len(set(roots)) != len(roots):
        raise admin.AdminError("Site profile datasets must not contain duplicate paths")
    for path in roots:
        try:
            info = Path(path).stat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise admin.AdminError("Dataset source must have one hard link: %s" % path)
    result = dict(profile)
    result["datasets"] = roots
    result["workspace"] = _path(profile["workspace"], "workspace", absolute=True)
    if result["workspace"] in roots:
        raise admin.AdminError("Workspace must be a directory separate from dataset source files")
    if "big_parent" in profile:
        result["big_parent"] = _path(profile["big_parent"], "big_parent", absolute=True)
    if "server" in profile:
        try:
            result["server"] = normalize_server(profile["server"])
        except ValueError as exc:
            raise admin.AdminError("Invalid site profile server: %s" % exc) from exc
    return result


def create_profile(path, datasets, workspace, *, big_parent=None, server=None):
    """Save an explicitly requested private profile, refusing any overwrite.

    Relative command-line paths become absolute; aliases are rejected before
    conversion. Only the profile and its requested parent directories are
    created. A separate workspace is left for the first operation that needs it.
    """
    if not isinstance(datasets, list) or not datasets:
        raise admin.AdminError("Select at least one datasets.xml path")
    target = _path(os.fspath(path), "profile output")
    profile = {"kind": PROFILE_KIND, "version": PROFILE_VERSION,
               "datasets": [_path(value, "datasets path") for value in datasets],
               "workspace": _path(os.fspath(workspace), "workspace")}
    if big_parent is not None:
        profile["big_parent"] = _path(os.fspath(big_parent), "big_parent")
    if server is not None:
        profile["server"] = server
    profile = validate_profile(profile)
    if target in profile["datasets"] or target == profile["workspace"]:
        raise admin.AdminError("Profile output must be separate from dataset sources and the workspace directory")
    if os.path.lexists(target):
        raise admin.AdminError("Site profile already exists; choose a new output path: %s" % target)
    admin.write_private(target, admin.encoded(profile))
    return profile


def load_profile(path):
    """Read the private document through the same regular-file checks as plans."""
    target = _path(os.fspath(path), "profile")
    value, _ = admin.load_document(target, PROFILE_KIND)
    return validate_profile(value)


def _accessible(path, mode):
    if os.access in os.supports_effective_ids:
        return os.access(path, mode, effective_ids=True)
    return os.access(path, mode)


def doctor(profile):
    """Return checks as ``{name, status, detail}`` without writing or networking.

    Access checks are advisory snapshots. No test files, locks, reload flags,
    staging files, or catalog requests are created by this operation.
    """
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    supported = sys.version_info >= (3, 9) and os.name == "posix"
    add("runtime", "ok" if supported else "error",
        "Python %s on %s; Python 3.9+ and POSIX are required." %
        (".".join(map(str, sys.version_info[:3])), os.name))
    limits = ("Read-only preflight cannot prove an ERDDAP semantic load or determine whether "
              "datasets.xml is generated. Confirm the active paths and pause configuration generators "
              "and other writers before applying. Permissions may change; apply rechecks its inputs. "
              "No files, reload flags, or remote requests were created.")
    try:
        profile = validate_profile(profile)
    except (admin.AdminError, OSError, ValueError) as exc:
        add("profile", "error", str(exc))
        add("limits", "warning", limits)
        return checks
    add("profile", "ok", "Explicit site paths and credential-free profile schema are valid.")

    files = [{"path": path} for path in profile["datasets"]]
    try:
        plan = build_plan(profile["datasets"], "__ERDDAP_PREFLIGHT_28A838_OLD__",
                          "__ERDDAP_PREFLIGHT_28A838_NEW__", case_sensitive=True)
        files = plan["files"]
        add("source graph", "ok", "Read %d source/include files and inventoried %d datasets; local XML and includes are supported." %
            (len(files), len(plan["datasets"])))
    except (PlanError, OSError, ValueError) as exc:
        add("source graph", "error", str(exc))

    directories = set()
    for item in files:
        path = item["path"]
        directories.add(str(Path(path).parent))
        try:
            data, state = admin.read_regular(path)
            if "before_sha256" in item and admin.sha256(data) != item["before_sha256"]:
                raise admin.AdminError("Input changed during preflight: %s" % path)
            add("source: " + path, "ok", "Readable regular source; mode %04o, uid %s, gid %s; passed the apply workflow's input safeguards." %
                (state["mode"], state["uid"], state["gid"]))
            uid = os.geteuid() if hasattr(os, "geteuid") else None
            groups = set(os.getgroups()) | {os.getegid()} if hasattr(os, "getgroups") else set()
            owner_supported = uid == 0 or (uid == state["uid"] and state["gid"] in groups)
            add("ownership: " + path, "ok" if owner_supported else "warning",
                "Current account appears able to preserve uid %s/gid %s; apply still verifies preservation." %
                (state["uid"], state["gid"]) if owner_supported else
                "Current account may be unable to preserve uid %s/gid %s when replacing this source. Use the source owner or an authorized administrator; no ownership changes were attempted." %
                (state["uid"], state["gid"]))
        except (admin.AdminError, OSError, ValueError) as exc:
            add("source: " + path, "error", str(exc))
    for directory in sorted(directories):
        writable = Path(directory).is_dir() and _accessible(directory, os.W_OK | os.X_OK)
        add("source directory: " + directory, "ok" if writable else "error",
            "Writable/searchable for staging and atomic replacement: %s" % directory if writable else
            "Cannot stage and replace source files; directory must exist and be writable/searchable: %s" % directory)

    workspace = Path(profile["workspace"])
    if workspace.exists():
        info = workspace.stat()
        if not stat.S_ISDIR(info.st_mode):
            add("workspace", "error", "Workspace must be an ordinary directory: %s" % workspace)
        elif stat.S_IMODE(info.st_mode) & 0o077:
            add("workspace", "error", "Existing workspace mode is %04o; use a private directory (0700): %s. Permissions were not changed." %
                (stat.S_IMODE(info.st_mode), workspace))
        elif not _accessible(workspace, os.W_OK | os.X_OK):
            add("workspace", "error", "Workspace is not writable/searchable by this account: %s" % workspace)
        else:
            add("workspace", "ok", "Private writable workspace; mode %04o, uid %s, gid %s: %s" %
                (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid, workspace))
        parent = workspace.parent
    else:
        add("workspace", "ok", "Workspace does not yet exist; an operation that needs artifacts can create it privately (0700): %s. Preflight did not create it." % workspace)
        parent = workspace.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
    parent_writable = parent.is_dir() and _accessible(parent, os.W_OK | os.X_OK)
    add("workspace parent", "ok" if parent_writable else ("warning" if workspace.is_dir() else "error"),
        "Nearest existing parent %s is %swritable/searchable; mode %04o. No directory was created." %
        (parent, "" if parent_writable else "not ", stat.S_IMODE(parent.stat().st_mode)))

    if "big_parent" not in profile:
        add("reload flags", "warning", "No big_parent configured; reload requests are unavailable. Configure the effective bigParentDirectory and its existing flag directory to enable them.")
    else:
        base = Path(profile["big_parent"])
        flags = base / "flag"
        try:
            _path(str(flags), "reload flag directory", absolute=True)
            if not base.is_dir() or not flags.is_dir():
                raise admin.AdminError("Expected the effective bigParentDirectory and its existing ordinary flag directory: %s" % flags)
            if not _accessible(flags, os.W_OK | os.X_OK):
                raise admin.AdminError("Reload flag directory is not writable/searchable: %s" % flags)
            add("reload flags", "ok", "Existing writable normal-reload directory: %s. No flags were created." % flags)
        except (admin.AdminError, OSError, ValueError) as exc:
            add("reload flags", "error", str(exc))
    add("server", "ok" if "server" in profile else "warning",
        "Optional catalog server: %s. URL syntax checked only; no remote requests were made." % profile["server"]
        if "server" in profile else "No public catalog server configured; local scans remain available.")
    add("limits", "warning", limits)
    return checks
