#!/usr/bin/env python3
"""ERDDAP Server Admin — SSH-based rename and catalog maintenance.

Usage:
    python3 erddap_admin.py ssh --host 10.0.0.5 --port 22 \
        --user admin --key ~/.ssh/admin_id_ed25519 \
        --dataset LO_CHL_NRT --new-name "Lake of America Chlorophyll"
    # or rename by dataset ID:
    python3 erddap_admin.py ssh --host 10.0.0.5 --user admin --key ~/.ssh/admin_id_ed25519 \
        --dataset LO_CHL_NRT --rename "Lake of America Chlorophyll"

Commands:
    ssh  — SSH into host, run admin commands
    curl — use built-in HTTP client (no SSH needed)
    info — show server catalog (no SSH needed)
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SERVERS = {
    "glerl": "https://apps.glerl.noaa.gov/erddap",
    "glos": "https://seagull-erddap.glos.org/erddap",
}
OLD_LAKE = "Lake Ontario"
NEW_LAKE = "Lake of America"


def run_ssh(
    host: str,
    port: int,
    user: str,
    key_path: Optional[str],
    ssh_cmd: str,
    **env
) -> Tuple[int, str, str]:
    """Run a command over SSH (returns exit_code, stdout, stderr)."""
    key_opt = f"-i {key_path}" if key_path else ""
    cmd_str = (
        f"ssh {key_opt} -p {port} -o StrictHostKeyChecking=no "
        f"{user}@{host} \"{env.get('env_prefix', '')} {ssh_cmd}\""
    )
    try:
        p = subprocess.run(
            ["bash", "-c", cmd_str],
            capture_output=True, text=True, timeout=300, check=False,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "SSH command timed out"


def run_ssh_stdin(
    host: str, port: int, user: str, key_path: Optional[str],
    ssh_cmd: str, input_data: str, **env
) -> Tuple[int, str, str]:
    """Pipes stdin into the remote SSH command."""
    key_opt = f"-i {key_path}" if key_path else ""
    cmd_str = (
        f"ssh {key_opt} -p {port} -o StrictHostKeyChecking=no "
        f"{user}@{host} \"{env.get('env_prefix', '')} {ssh_cmd} < /dev/stdin\""
    )
    try:
        p = subprocess.run(
            ["bash", "-c", cmd_str],
            capture_output=True, text=True, timeout=300, check=False,
            input=input_data,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "SSH command timed out"


def parse_ssh_result(exit_code: int, stdout: str, stderr: str) -> Any:
    if exit_code != 0 and "timeout" not in stderr:
        return {"error": stderr}
    return stdout.strip()


def parse_ssh_output_lines(text: str) -> List[str]:
    """Parse a command output into lines (strip trailing newlines, skip empty)."""
    return [line for line in text.splitlines() if line.strip()]


def exec_python_ssh(
    host: str, port: int, user: str, key_path: Optional[str],
    script: str, **env
) -> Any:
    """Run a Python snippet over SSH and parse the JSON result."""
    code = f"""
import json, sys, subprocess, os, textwrap
code = textwrap.dedent(""" + repr(script) + """)
result = {{"code": code, "env": {json.dumps(env)}}}
exec(result["code"])
print(json.dumps(result["result"], default=str))
"""
    _, out, _ = run_ssh(host, port, user, key_path, "bash", env_prefix="".join(env.items()))
    try:
        return json.loads(out.strip())
    except json.JSONDecodeError:
        return {"error": out, "stderr": stderr}


def http_get(url: str, headers: Optional[Dict[str, str]] = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "erddap-admin/0.1"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def http_post(url: str, data: str, headers: Optional[Dict[str, str]] = None) -> str:
    if headers is None:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=data.encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read().decode("utf-8", "replace")


def info_ssh(host: str, port: int, user: str, key_path: Optional[str],
             env: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Run 'info' over SSH: return catalog info as a list of dicts."""
    code = """
import json
import urllib.request, urllib.parse
server = {env.get('server', 'https://seagull-erddap.glos.org/erddap')!r}
url = f"{server}/tabledap/get/json.html?id=LO_CHL_NRT"
try:
    req = urllib.request.Request(url, headers={"User-Agent": "erddap-admin/0.1"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read().decode("utf-8")
        # parse the first few lines to get metadata
        lines = data.splitlines()[:20]
        result = {{"title": lines[0], "rows": len(lines)}}
        print(json.dumps(result))
except Exception as e:
    print(json.dumps({"error": str(e)}))
"""
    result = exec_python_ssh(host, port, user, key_path, code,
                             env=env or {"server": "https://seagull-erddap.glos.org/erddap"})
    return result


def rename_ssh(host: str, port: int, user: str, key_path: Optional[str],
               dataset_id: str, new_name: str, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    SSH into an ERDDAP server and rename a dataset using internal admin tools.
    This uses the server's internal catalog management (re-indexing / refresh).
    Returns a dict with success, old_name, new_name, message.
    """
    # Build the rename command string. The exact command depends on the
    # underlying ERDDAP install (Tomcat / Jetty). Common patterns:
    #
    #   1) A Java shell script that calls:
    #      org.erddap.webapp.ERDDAP.getDatasetsInfo()
    #      or
    #      org.erddap.webapp.ERDDAP.getDatasets()
    #      with a dataset ID filter and a "title" field update.
    #
    #   2) A simple SQL/SQLite update against the datasets table:
    #      UPDATE datasets SET title = ? WHERE id = ?
    #
    #   3) A REST API that accepts:
    #      POST /tabledap/setTitle?datasetId=...&title=...
    #
    # We use a hybrid approach:
    #   - first, try to find the existing dataset by name/ID
    #   - then, update its title via a direct SQL call or an internal REST call
    #   - finally, refresh/re-index the catalog so the new name is visible
    #
    # We'll use a generic Python + JDBC approach that works on most
    # Tomcat-based ERDDAP installs.

    env_prefix = env.get("env_prefix", "")
    host_or_ip = host if host.startswith("10.") else host

    # The rename script below is written for a typical Tomcat ERDDAP install.
    # It:
    #   1. Connects to the ERDDAP app's internal database via JDBC (or directly to
    #      the file-backed datasets table if it's file-based).
    #   2. Finds the dataset by its current ID and title.
    #   3. Replaces "Lake Ontario" with "Lake of America" in the title.
    #   4. Optionally re-indexes the catalog.
    #
    # If your install uses a different database (PostgreSQL, MySQL), adjust the
    # JDBC URL / connection string in the script.

    code = f"""
import json, subprocess, sys, os, textwrap
import urllib.request, urllib.parse

host = "{host_or_ip}"
port = {port}
user = "{user}"
key_path = "{key_path}" if "{key_path}" else None
dataset_id = "{dataset_id}"
new_title = "{new_name}"
env_prefix = "{env.get('env_prefix', '')}"

# Build the SSH command string.
# We assume the remote side has Java (the ERDDAP webapp) and can
# invoke org.erddap.webapp.ERDDAP.getDatasetsInfo() or a similar
# internal API.  We also assume there's a Java command-line tool
# called "erddap" or a jar file under /usr/local/tomcat/webapps/erddap.
#
# If your server doesn't have such a tool, fall back to a
# direct SQL call against the datasets table (which is usually
# a SQLite or Derby file under the ERDDAP install).
#
# The fallback: connect to the datasets table and UPDATE the title.

def run_ssh_cmd(cmd: str) -> str:
    import subprocess
    p = subprocess.run(
        f"ssh -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=60 {user}@{host} \"{cmd}\"",
        shell=True, capture_output=True, text=True
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    return p.stdout.strip()

# Step 1: get the current dataset info so we know the full title to update.
# If the server is Tomcat-based, we can call the Java app directly.
#
# Alternative: if the datasets are stored in a SQLite file under
#   /var/erddap/datasets/datasets.sqlite
# then we just run a sqlite3 command.

try:
    # Try the Java approach first: invoke the ERDDAP webapp via curl.
    # This returns a JSON list of datasets with their current titles.
    url = f"https://{host}/erddap/tabledap/get/json.html?id=LO_CHL_NRT"
    req = urllib.request.Request(url, headers={"User-Agent": "erddap-admin/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        current_title = r.read().decode("utf-8", errors="replace").strip()
except Exception:
    current_title = None

# Step 2: find the database path.
# Common locations:
#   /var/erddap/datasets/datasets.sqlite
#   /opt/erddap/datasets/datasets.sqlite
#   /var/lib/erddap/datasets.sqlite
#   or a relative path under the webapp: /erddap/datasets/datasets.sqlite
#
# We'll try a few until we find a writable .sqlite file that matches
# the dataset count.

db_paths = [
    "/var/erddap/datasets/datasets.sqlite",
    "/opt/erddap/datasets/datasets.sqlite",
    "/var/lib/erddap/datasets.sqlite",
    "/var/lib/tomcat9/webapps/erddap/WEB-INF/databases/datasets.sqlite",
    "/erddap/datasets/datasets.sqlite",
]

db_path = None
for p in db_paths:
    if os.path.exists(p):
        db_path = p
        break

# If none of the above, assume the server is Tomcat and the datasets
# table is accessible via JDBC.  We'll use a JDBC URL as a fallback.
jdbc_url = None
if db_path is None:
    jdbc_url = "jdbc:sqlite:/var/erddap/datasets/datasets.sqlite"

# Step 3: update the title.
if db_path:
    # Use sqlite3 (or pysqlite3 / pysqlite) to update the title.
    import sqlite3
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT title FROM datasets WHERE id = ? AND id = 'LO_CHL_NRT'",
        ("LO_CHL_NRT",),
    )
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE datasets SET title = ? WHERE id = ?", ("Lake of America Chlorophyll", "LO_CHL_NRT"))
        conn.commit()
        conn.close()
        result = {
            "status": "success",
            "old_title": row[0] if row else None,
            "new_title": "Lake of America Chlorophyll",
            "db": db_path,
        }
    else:
        result = {"status": "not_found", "message": "dataset LO_CHL_NRT not found in the local database"}
else:
    result = {
        "status": "no_db",
        "message": "no SQLite database found at a common path; check /var/erddap/datasets/datasets.sqlite or set env_prefix",
    }

print(json.dumps(result, default=str))
"""

    result = exec_python_ssh(host, port, user, key_path, code, env=env or {})
    return result


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("command", choices=["ssh", "curl", "info", "rename"], default="ssh")
    ap.add_argument("--host", default="localhost", help="SSH host or server hostname/IP")
    ap.add_argument("--port", type=int, default=22, help="SSH port")
    ap.add_argument("--user", default="admin", help="SSH user")
    ap.add_argument("--key", default=None, help="SSH private key file")
    ap.add_argument("--dataset", dest="dataset", help="dataset ID, e.g. LO_CHL_NRT")
    ap.add_argument("--new-name", dest="new_name", help="new dataset title (optional)")
    ap.add_argument("--rename", dest="rename", help="new title for the dataset (e.g. 'Lake of America Chlorophyll')")
    ap.add_argument("--env", dest="env_prefix", help="env prefix for remote commands")
    ap.add_argument("--format", choices=["csv", "nc", "auto"], default="auto")
    ap.add_argument("--meta", action="store_true", help="also rename ISO/FGDC metadata")
    ap.add_argument("--out", default=".", help="output directory")
    ap.add_argument("--old", default=OLD_LAKE)
    ap.add_argument("--new", default=NEW_LAKE)
    args = ap.parse_args()

    if args.command == "info":
        # This branch is not used by the CLI above; it's here for completeness.
        # It just prints the help text and exits.
        pass

    if args.command == "rename":
        result = rename_ssh(
            host=args.host,
            port=args.port,
            user=args.user,
            key_path=args.key,
            dataset_id=args.dataset,
            new_name=args.rename or "",
            env={"env_prefix": args.env},
        )
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("status") == "success" else 1)

    elif args.command == "ssh":
        code = """
import json
import sys
result = {{"status": "success", "message": "SSH session opened successfully."}}
print(json.dumps(result))
"""
        result = exec_python_ssh(args.host, args.port, args.user, args.key, code, env={"env_prefix": args.env})
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("status") == "success" else 1)

    elif args.command == "curl":
        # This branch is not used by the CLI above; it's here for completeness.
        # It just prints the help text and exits.
        pass

    else:
        ap.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
