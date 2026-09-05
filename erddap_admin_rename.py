#!/usr/bin/env python3
"""ERDDAP Server Rename Admin Tool — with pull mode.

SSH into an ERDDAP server and rename all references of a lake
(e.g. "Lake Ontario" -> "Lake of America") across metadata files,
database tables, and configuration files.

Usage:
    # Dry-run: scan without modifying
    python3 erddap_admin_rename.py --host <host> --user admin --key ~/.ssh/admin_key --dry-run

    # Pull: fetch metadata records and display them for review
    python3 erddap_admin_rename.py --host <host> --user admin --key ~/.ssh/admin_key \
        --pull --pull-dir /tmp/pull-output

    # Apply: rewrite files in place
    python3 erddap_admin_rename.py --host <host> --user admin --key ~/.ssh/admin_key --apply
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

# Paths that are excluded from scanning.
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


def run_python_ssh(
    host: str, port: int, user: str, key_path: Optional[str],
    python_code: str, env_prefix: Optional[str] = None,
) -> Any:
    """Run arbitrary Python code over SSH and return its stdout as JSON/text."""
    cmd = f"python3 -c \"{python_code}\""
    exit_code, stdout, stderr = run_bash_ssh(host, port, user, key_path, command=cmd, env_prefix=env_prefix)
    return parse_ssh_output(stdout)


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
    exit_code, stdout, stderr = run_ssh(host, 22, "root", None, command=cmd)
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


def fetch_metadata_from_api(server: str, dataset_id: str) -> Tuple[Optional[Dict], Optional[str]]:
    """
    Fetch a dataset's metadata from the ERDDAP server using the REST API.
    Returns (metadata_dict_or_none, error_message_or_none).
    """
    try:
        url = f"{server.rstrip('/')}/metadata/xml/{dataset_id}_iso19115.xml"
        req = urllib.request.Request(url, headers={"Accept": "application/xml"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="replace"), None
            else:
                return None, f"HTTP {resp.status} from {url}"
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} for {url}"
    except urllib.error.URLError as e:
        return None, f"Network error: {e.reason}"
    except Exception as e:
        return None, str(e)


def pull_all_metadata(server: str, search_term: str) -> List[Dict[str, Any]]:
    """
    Use the ERDDAP search API to find all datasets containing the search term,
    then fetch and display their metadata.

    Returns a list of dicts with: {dataset_id, title, url, metadata_xml}
    """
    results: List[Dict[str, Any]] = []
    search_url = f"{server.rstrip('/')}/search/index.html?searchFor={search_term}"
    try:
        req = urllib.request.Request(search_url, headers={"Accept": "text/html,application/xhtml+xml"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return [{"error": str(e)}]

    # Extract (datasetID, title) pairs from the HTML.
    dataset_ids: List[str] = []
    for m in re.finditer(r'id="([A-Za-z0-9_.\-]+)"[^>]*title="([^"]*)"', html):
        dataset_ids.append(m.group(1))

    if not dataset_ids:
        # Fallback: parse the HTML table rows.
        for m in re.finditer(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL):
            row = m.group(1)
            id_match = re.search(r'id="([A-Za-z0-9_.\-]+)"', row)
            title_match = re.search(r'<[^>]*title="([^"]*)"', row)
            if id_match and title_match:
                dataset_ids.append(id_match.group(1))

    for did in dataset_ids:
        metadata_xml, err = fetch_metadata_from_api(server, did)
        if err:
            results.append({"dataset_id": did, "error": err})
        elif metadata_xml:
            results.append({
                "dataset_id": did,
                "title": re.search(r'<title[^>]*>([^<]+)</title>', metadata_xml).group(1) if re.search(r'<title[^>]*>([^<]+)</title>', metadata_xml) else did,
                "url": f"{server.rstrip('/')}/metadata/xml/{did}_iso19115.xml",
                "metadata_xml": metadata_xml[:10000] + "..." if len(metadata_xml) > 10000 else metadata_xml,
            })

    return results


def pull_database(host: str, db_path: str) -> List[Dict[str, Any]]:
    """
    Pull the contents of a database file via SSH (read-only).
    Returns a list of records containing the search term.
    """
    results: List[Dict[str, Any]] = []
    exit_code, stdout, stderr = run_bash_ssh(host, 22, "root", None, f"cat '{db_path}' 2>/dev/null")
    if exit_code != 0:
        return [{"error": f"Failed to read {db_path}: {stderr}"}]
    text = stdout
    # Simple: find lines containing the search term.
    for line in text.splitlines():
        if OLD_LAKE.lower() in line.lower():
            results.append({"line": line.strip()})
    return results


def pull_webpage(host: str, path: str) -> Optional[str]:
    """
    Pull a single file from the remote host via HTTP (not SSH).
    Returns the raw text or None on error.
    """
    url = f"https://{host.rstrip('/')}/{path.lstrip('/')}".rstrip("/")
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/xml,application/xml,text/plain,*/*"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="replace")
            return None
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description="ERDDAP Server Rename Admin Tool — with pull mode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--host", required=True, help="ERDDAP server hostname or IP")
    ap.add_argument("--user", default="root", help="SSH user")
    ap.add_argument("--key", default=None, help="SSH private key file")
    ap.add_argument("--port", type=int, default=22, help="SSH port")
    ap.add_argument("--old", default=OLD_LAKE, help="old lake name")
    ap.add_argument("--new", default=NEW_LAKE, help="new lake name")
    ap.add_argument("--dry-run", action="store_true", help="preview changes without modifying files")
    ap.add_argument("--apply", action="store_true", help="apply changes (default if neither --dry-run nor --pull is set)")
    ap.add_argument("--backup-dir", default="/tmp/erddap-backup", help="backup directory")
    ap.add_argument("--server", help="override the default ERDDAP server URL")
    ap.add_argument("--scan-only", action="store_true", help="only scan, don't rewrite")
    ap.add_argument("--pull", action="store_true", help="pull metadata records and display them for review")
    ap.add_argument("--pull-db", action="store_true", help="pull database content and display for review")
    ap.add_argument("--pull-dir", default="/tmp/pull-output", help="directory to write pulled records to")
    args = ap.parse_args()

    old_lake = args.old
    new_lake = args.new

    server = args.server or DEFAULT_SERVERS[0]
    base_url = server.rstrip("/")

    if args.key:
        print(f"  Using SSH key: {args.key}")
    else:
        print("  WARNING: No SSH key provided. Falling back to password prompt.")
        print("  If you are not an admin, you cannot SSH into the server.")

    ssh_cmd = (
        f"ssh -i {args.key} -p {args.port} -o StrictHostKeyChecking=no "
        f"-o BatchMode=yes -o ConnectTimeout=300 {args.user}@{args.host}"
    ) if args.key else (
        f"ssh -p {args.port} -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=300 {args.user}@{args.host}"
    )

    # ─── Mode: --pull (read-only fetch of metadata records) ────────────────────
    if args.pull:
        print(f"\n[Pull Mode] Fetching metadata records for '{old_lake}' from {server}...")
        results = pull_all_metadata(server, old_lake)
        for r in results:
            if "error" in r:
                print(f"  Error: {r['error']}")
            else:
                print(f"\n{'='*70}")
                print(f"  Dataset ID: {r['dataset_id']}")
                print(f"  Title: {r['title']}")
                print(f"  Metadata URL: {r['url']}")
                print(f"{'='*70}")
                print(f"  Metadata XML (first 10k chars):\n{r['metadata_xml']}")
                print(f"\n  Full metadata saved to: {args.pull_dir}/{r['dataset_id']}.xml")
                # Also save the full metadata XML to disk.
                full_path = Path(args.pull_dir) / f"{r['dataset_id']}.xml"
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(r['metadata_xml'])
        print(f"\n[Pull Mode] Done. Records saved to {args.pull_dir}/")
        sys.exit(0)

    # ─── Mode: --pull-db (read-only fetch of database) ───────────────────────
    if args.pull_db:
        print(f"\n[Pull DB Mode] Fetching database content from {args.host}...")
        results = pull_database(args.host, "/var/lib/erddap/db/erddap.db")
        for r in results:
            if "error" in r:
                print(f"  Error: {r['error']}")
            else:
                print(f"  {r['line']}")
        print(f"\n[Pull DB Mode] Done.")
        sys.exit(0)

    # ─── Mode: --dry-run (scan without modifying) ────────────────────────────
    if args.dry_run or not args.apply:
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

        # ─── Step 2: Scan for occurrences ─────────────────────────────────────
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

        print(f"\n[Step 3] DRY-RUN: Would rewrite {len(occurrences)} files.")
        sys.exit(0)

    # ─── Apply rewrites ───────────────────────────────────────────────────────
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

    # ─── Step 2: Scan for occurrences ───────────────────────────────────────
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

    # ─── Step 3: Apply rewrites ──────────────────────────────────────────────
    print(f"\n[Step 3] Applying rewrites...")
    for path, count, text in occurrences:
        backup_file(path, args.backup_dir)
        new_text = text.replace(old_lake, new_lake)
        if new_text == text:
            print(f"  {path}: no changes needed (case-insensitive match already applied)")
            continue
        write_bytes_ssh(args.host, path, new_text.encode("utf-8"))
        print(f"  {path}: {count} occurrence(s) rewritten")

    print(f"\n[Step 3] Done. Rewrote {len(occurrences)} files.")
    sys.exit(0)


if __name__ == "__main__":
    main()
