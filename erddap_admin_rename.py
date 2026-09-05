#!/usr/bin/env python3
"""ERDDAP Server Rename Admin Tool.

SSH into an ERDDAP server and rename all references of a lake (e.g.
"Lake Ontario" -> "Lake of America") across:
  - metadata files (XML, CSV, JSON, properties, yml, ini, config)
  - database tables (SQLite, Derby, H2)
  - webapp source files (Java, XML config)
  - any text file under common ERDDAP paths

Usage:
    python3 erddap_admin_rename.py --host <host> --user <user> --key <keyfile> \\
        --old "Lake Ontario" --new "Lake of America" --dry-run
    python3 erddap_admin_rename.py --host <host> --user <user> --key <keyfile> \\
        --old "Lake Ontario" --new "Lake of America" --apply
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ─── Configuration ────────────────────────────────────────────────────────────
OLD_LAKE = "Lake Ontario"
NEW_LAKE = "Lake of America"
DEFAULT_SERVERS = [
    "https://apps.glerl.noaa.gov/erddap",
    "https://seagull-erddap.glos.org/erddap",
]

# Common ERDDAP install locations to scan.
ROOT_PATHS = [
    "/opt/erddap",
    "/var/erddap",
    "/srv/erddap",
    "/opt/erddap-server",
    "/var/lib/erddap",
    "/home/erddap",
    "/app/erddap",
    "/erddap",
]

# Paths that are *excluded* from scanning.
EXCLUDE_PATHS = [
    "/var/log",
    "/tmp",
    "/var/tmp",
    "/var/cache",
    "/opt/conda",
    "/opt/miniconda",
    "/opt/miniconda3",
    "/usr/lib",
    "/usr/share",
]

# Metadata file patterns to scan.
METADATA_EXTENSIONS = (
    ".xml", ".xml.gz", ".json", ".json.gz", ".jsonl", ".jsonl.gz",
    ".csv", ".csv.gz", ".tsv", ".tsv.gz", ".properties", ".props",
    ".yml", ".yaml", ".yaml.gz", ".ini", ".conf", ".conf.gz",
    ".properties.gz", ".toml", ".toml.gz", ".xml.gz",
)

# Database file patterns.
DB_EXTENSIONS = (".sqlite", ".sqlite3", ".db", ".db3", ".derby", ".h2",)

# Database dialect mapping.
DB_DIALECTS = {
    ".sqlite": "sqlite",
    ".sqlite3": "sqlite",
    ".db": "sqlite",
    ".db3": "sqlite",
    ".h2": "h2",
    ".derby": "derby",
}


def run_bash_ssh(
    host: str, port: int, user: str, key_path: Optional[str],
    bash_command: str, env_prefix: Optional[str] = None,
) -> Tuple[int, str, str]:
    """Run a bash command over SSH and return (exit_code, stdout, stderr)."""
    key_opt = f"-i {key_path}" if key_path else ""
    ssh_cmd = (
        f"ssh {key_opt} -p {port} -o StrictHostKeyChecking=no "
        f"-o BatchMode=yes -o ConnectTimeout=300 {user}@{host} "
        f"'{env_prefix or ''} {bash_command}'"
    )
    try:
        result = subprocess.run(
            ["bash", "-c", ssh_cmd],
            capture_output=True, text=True, timeout=300, check=False,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "SSH command timed out"


def parse_ssh_output(text: str) -> Any:
    """Parse a JSON or plain-text SSH output into a Python object."""
    if not text.strip():
        return None
    if text.strip().startswith("{") or text.strip().startswith("["):
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass
    return text.strip()


def run_python_ssh(
    host: str, port: int, user: str, key_path: Optional[str],
    python_code: str, env_prefix: Optional[str] = None,
) -> Any:
    """Run arbitrary Python code over SSH and return its stdout as JSON/text."""
    cmd = f"python3 -c \"{python_code}\""
    exit_code, stdout, stderr = run_bash_ssh(host, port, user, key_path, command=cmd, env_prefix=env_prefix)
    return parse_ssh_output(stdout)


def is_excluded(path: str) -> bool:
    """Check whether a path is excluded from scanning."""
    for excl in EXCLUDE_PATHS:
        if excl in path:
            return True
    return False


def file_checksum(path: str) -> Optional[str]:
    """Return the MD5 checksum of a file, or None if it doesn't exist."""
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return None


def backup_file(path: str, backup_dir: str) -> str:
    """Backup a single file to a backup directory, preserving structure."""
    backup_dir = Path(backup_dir).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    rel_path = Path(path).relative_to(backup_dir.parent)
    dest = backup_dir / rel_path
    try:
        shutil.copy2(path, dest)
    except shutil.SameFileError:
        pass
    except OSError:
        pass
    return str(dest)


def scan_paths(host: str) -> List[str]:
    """
    Scan a remote ERDDAP host for candidate metadata and config files.
    Returns a list of absolute (remote) paths to scan.
    """
    paths = []
    for root in ROOT_PATHS:
        scan_root = f"/{root}" if not root.startswith("/") else root
        try:
            exit_code, stdout, _ = run_bash_ssh(
                host, 22, "root", None,
                f"find {scan_root} -maxdepth 3 -type f "
                f"-name '*.{','.join(METADATA_EXTENSIONS)}' "
                f"-o -name '*.{','.join(DB_EXTENSIONS)}' "
                f"2>/dev/null | sort",
            )
            if exit_code == 0:
                for line in stdout.strip().splitlines():
                    line = line.strip()
                    if line:
                        paths.append(line)
        except Exception:
            continue
    # Also scan the webapp directory for configuration files.
    try:
        exit_code, stdout, _ = run_bash_ssh(
            host, 22, "root", None,
            f"find /opt/erddap -type f -name '*.xml' -o -name '*.properties' 2>/dev/null | sort",
        )
        if exit_code == 0:
            for line in stdout.strip().splitlines():
                line = line.strip()
                if line:
                    paths.append(line)
    except Exception:
        pass
    return sorted(set(paths))


def rewrite_metadata_file(path: str, text: str) -> str:
    """
    Rewrite a metadata file in place (or return new bytes).
    We handle:
      - XML: replace text content between tags (not tag names or attributes).
      - JSON: replace string values in object/array values (not keys).
      - CSV/TSV: replace in header comments and data rows.
      - YAML/TOML/INI: simple string replacement in non-key parts.
      - Properties: simple string replacement in values (not keys).
    """
    import json
    import re

    ext = os.path.splitext(path)[-1].lower()

    if ext in (".xml", ".xml.gz"):
        return _rewrite_xml(text)
    if ext == ".json" or ext == ".jsonl":
        return _rewrite_json(text)
    if ext == ".csv" or ext == ".tsv":
        return _rewrite_csv(text)
    if ext in (".yml", ".yaml"):
        return _rewrite_yaml(text)
    if ext in (".properties", ".props"):
        return _rewrite_properties(text)
    if ext in (".ini", ".conf", ".toml"):
        return _rewrite_ini(text)
    # Fallback: simple text replacement.
    new_text = text.replace(OLD_LAKE, NEW_LAKE)
    return new_text


def _rewrite_xml(text: str) -> str:
    """Rewrite XML metadata files."""
    pattern = re.compile(r"<[^>]*>([^<]*)</[^>]*>", re.DOTALL)
    result = text
    for m in pattern.finditer(text):
        inner = m.group(1).strip()
        if OLD_LAKE in inner:
            new_inner = inner.replace(OLD_LAKE, NEW_LAKE)
            if new_inner != inner:
                result = result[: m.start()] + new_inner + result[m.end():]
    return result


def _rewrite_json(text: str) -> str:
    """Rewrite JSON metadata by replacing strings in values (not keys)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    def replacer(obj):
        if isinstance(obj, str):
            return obj.replace(OLD_LAKE, NEW_LAKE)
        elif isinstance(obj, dict):
            return {k: replacer(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [replacer(item) for item in obj]
        else:
            return obj
    new_data = replacer(data)
    return json.dumps(new_data, indent=2, ensure_ascii=False)


def _rewrite_csv(text: str) -> str:
    """Rewrite CSV metadata."""
    lines = text.splitlines()
    new_lines = []
    for line in lines:
        if line.startswith("#"):
            new_lines.append(line.replace(OLD_LAKE, NEW_LAKE))
        else:
            parts = line.split(",", 5)
            new_parts = []
            for part in parts:
                if part.strip():
                    new_parts.append(part.replace(OLD_LAKE, NEW_LAKE))
                else:
                    new_parts.append(part)
            new_lines.append(",".join(new_parts))
    return "\n".join(new_lines)


def _rewrite_yaml(text: str) -> str:
    """Rewrite YAML metadata."""
    lines = text.splitlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            new_lines.append(line.replace(OLD_LAKE, NEW_LAKE))
        elif stripped and not stripped.startswith("-") and ":" in stripped:
            key, sep, value = stripped.partition(":")
            if key.strip() and not any(x in key for x in (OLD_LAKE, NEW_LAKE)):
                new_value = value.replace(OLD_LAKE, NEW_LAKE)
                new_lines.append(f"{key.strip()}:{new_value}")
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)
    return "\n".join(new_lines)


def _rewrite_properties(text: str) -> str:
    """Rewrite a .properties file."""
    lines = text.splitlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("!"):
            new_lines.append(line.replace(OLD_LAKE, NEW_LAKE))
        elif stripped.startswith("-"):
            new_lines.append(line)
        elif stripped.startswith("###") or stripped.startswith("---"):
            new_lines.append(line)
        elif stripped and "=" in stripped:
            key, sep, value = stripped.partition("=")
            new_value = value.replace(OLD_LAKE, NEW_LAKE)
            new_lines.append(f"{key.strip()}={new_value}")
        else:
            new_lines.append(line)
    return "\n".join(new_lines)


def _rewrite_ini(text: str) -> str:
    """Rewrite an INI/TOML-like config file."""
    lines = text.splitlines()
    new_lines = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = True
            new_lines.append(line)
            continue
        elif stripped.endswith("]"):
            in_section = True
            new_lines.append(line)
            continue
        if stripped.startswith("#") or stripped.startswith(";"):
            new_lines.append(line.replace(OLD_LAKE, NEW_LAKE))
            continue
        if stripped.startswith("-"):
            new_lines.append(line)
            continue
        if stripped.startswith("###") or stripped.startswith("---"):
            new_lines.append(line)
            continue
        if stripped and "=" in stripped:
            key, sep, value = stripped.partition("=")
            new_value = value.replace(OLD_LAKE, NEW_LAKE)
            new_lines.append(f"{key.strip()}={new_value}")
        elif stripped and ":" in stripped:
            key, sep, value = stripped.partition(":")
            new_value = value.replace(OLD_LAKE, NEW_LAKE)
            new_lines.append(f"{key.strip()}:{new_value}")
        else:
            new_lines.append(line)
    return "\n".join(new_lines)


def write_bytes_ssh(host: str, path: str, data: bytes) -> None:
    """Write bytes to a file on the remote host via SSH."""
    import base64
    encoded = base64.b64encode(data).decode("ascii")
    cmd = f"mkdir -p `dirname '{path}'` && printf '%s' '{encoded}' | base64 -d > '{path}'"
    exit_code, stdout, stderr = run_bash_ssh(host, 22, "root", None, command=cmd)
    if exit_code != 0:
        raise RuntimeError(f"Failed to write {path}: {stderr}")


def rewrite_database(host: str, db_path: str) -> List[Dict[str, Any]]:
    """
    Rewrite a database file in place via SSH.
    Returns a list of changes made.
    """
    changes: List[Dict[str, Any]] = []
    ext = os.path.splitext(db_path)[-1].lower()
    dialect = DB_DIALECTS.get(ext, "unknown")

    if dialect == "sqlite":
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]
        for tbl in tables:
            cursor.execute(f"PRAGMA table_info({tbl});")
            columns = [row[1] for row in cursor.fetchall()]
            for col in columns:
                cursor.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE LOWER({col}) LIKE '%{OLD_LAKE.lower()}%';",
                )
                count = cursor.fetchone()[0]
                if count > 0:
                    cursor.execute(
                        f"UPDATE {tbl} SET {col} = REPLACE({col}, ?, ?) WHERE {col} LIKE ?;",
                        (OLD_LAKE, NEW_LAKE, f"%{OLD_LAKE}%"),
                    )
                    changes.append({
                        "table": tbl,
                        "column": col,
                        "rows_affected": count,
                    })
        conn.commit()
        conn.close()
        return changes
    else:
        return [{"db": db_path, "dialect": dialect, "status": "skipped"}]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="ERDDAP Server Rename Admin Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--host", required=True, help="ERDDAP server hostname or IP")
    ap.add_argument("--user", default="root", help="SSH user")
    ap.add_argument("--key", default=None, help="SSH private key file")
    ap.add_argument("--port", type=int, default=22, help="SSH port")
    ap.add_argument("--old", default=OLD_LAKE, help="old lake name")
    ap.add_argument("--new", default=NEW_LAKE, help="new lake name")
    ap.add_argument("--dry-run", action="store_true", help="preview changes without applying")
    ap.add_argument("--apply", action="store_true", help="apply changes (default if --dry-run is not set)")
    ap.add_argument("--backup-dir", default="/tmp/erddap-backup", help="backup directory")
    ap.add_argument("--server", help="override the default ERDDAP server URL")
    ap.add_argument("--scan-only", action="store_true", help="only scan, don't rewrite")
    args = ap.parse_args()

    old_lake = args.old
    new_lake = args.new

    # Determine the ERDDAP server URL.
    server = args.server or DEFAULT_SERVERS[0]
    base_url = server.rstrip("/")

    # Connect via SSH.
    if args.key:
        print(f"  Using SSH key: {args.key}")
    else:
        print("  WARNING: No SSH key provided. Falling back to password prompt.")
        print("  If you are not an admin, you cannot SSH into the server.")

    # Build the full SSH command string for later use.
    key_opt = f"-i {args.key}" if args.key else ""
    ssh_cmd = (
        f"ssh {key_opt} -p {args.port} -o StrictHostKeyChecking=no "
        f"-o BatchMode=yes -o ConnectTimeout=300 {args.user}@{args.host}"
    )

    # ─── Step 1: Scan for files ───────────────────────────────────────────────
    print(f"\n[Step 1] Scanning paths on {args.host}...")
    all_files: List[str] = []
    exit_code, stdout, stderr = run_bash_ssh(
        args.host, args.port, args.user, args.key,
        f"find / -type f \\( -name '*.{','.join(METADATA_EXTENSIONS)}' -o -name '*.{','.join(DB_EXTENSIONS)}' \\) "
        f"2>/dev/null | sort",
    )
    if exit_code != 0:
        print(f"  find failed: {stderr}")
        sys.exit(1)
    all_files.extend(stdout.strip().splitlines())
    all_files = [p.strip() for p in all_files if p.strip()]
    all_files = [p for p in all_files if not p.startswith("/var/log") and not p.startswith("/tmp")]
    print(f"  Found {len(all_files)} candidate files.")

    # ─── Step 2: Scan for occurrences ─────────────────────────────────────────
    print(f"\n[Step 2] Scanning for '{old_lake}' references...")
    occurrences: List[Tuple[str, int, str]] = []
    for path in all_files:
        if is_excluded(path):
            continue
        try:
            exit_code, stdout, _ = run_bash_ssh(
                args.host, args.port, args.user, args.key,
                f"cat '{path}' 2>/dev/null",
            )
            if exit_code != 0:
                continue
            text = stdout
            if old_lake.lower() in text.lower():
                count = text.lower().count(old_lake.lower())
                occurrences.append((path, count, text))
        except Exception as e:
            print(f"  Error reading {path}: {e}")
            continue

    print(f"  Found {len(occurrences)} files containing '{old_lake}':")
    for path, count, _ in occurrences[:30]:
        print(f"    {path} — {count} occurrence(s)")
    if len(occurrences) > 30:
        print(f"    ... and {len(occurrences) - 30} more.")

    # ─── Step 3: Rewrite (dry-run or apply) ───────────────────────────────────
    if args.dry_run or not args.apply:
        print(f"\n[Step 3] DRY-RUN: Would rewrite {len(occurrences)} files.")
        for path, count, text in occurrences[:5]:
            new_text = text.replace(old_lake, new_lake)
            if new_text != text:
                diff_lines = len([ln for ln in new_text.splitlines() if ln != ln])
                print(f"  {path}: {count} -> {len(new_text.splitlines())} lines")
        sys.exit(0)

    # ─── Apply rewrites ───────────────────────────────────────────────────────
    print(f"\n[Step 3] Applying rewrites...")
    for path, count, text in occurrences:
        # Backup.
        backup_file(path, args.backup_dir)

        # Rewrite.
        new_text = text.replace(old_lake, new_lake)
        if new_text == text:
            print(f"  {path}: no changes needed (case-insensitive match already applied)")
            continue

        # Write back.
        write_bytes_ssh(args.host, path, new_text.encode("utf-8"))
        print(f"  {path}: {count} occurrence(s) rewritten")

    print(f"\n[Step 3] Done. Rewrote {len(occurrences)} files.")
    sys.exit(0)


if __name__ == "__main__":
    main()
