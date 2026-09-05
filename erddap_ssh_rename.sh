#!/usr/bin/env bash
# erddap_ssh_rename.sh — sysadmin find/replace on an ERDDAP host you administer.
#
# Default is READ-ONLY: scan and report, write nothing.
# Pass --apply to rewrite files (a timestamped .bak is created first).
#
# Use only on servers you are authorized to administer. Host-key checking stays
# on. Passwords are not accepted on the CLI — use an SSH key or agent.
#
#   ./erddap_ssh_rename.sh --host erddap.example.edu --user erddap
#   ./erddap_ssh_rename.sh --host erddap.example.edu --user erddap --apply
#   ./erddap_ssh_rename.sh --local ./fixtures/erddap-content
#
set -euo pipefail

OLD_DEFAULT="Lake Ontario"
NEW_DEFAULT="Lake of America"

HOST=""
USER_NAME="${USER:-erddap}"
PORT="22"
IDENTITY=""
LOCAL_DIR=""
APPLY=0
OLD="$OLD_DEFAULT"
NEW="$NEW_DEFAULT"
ROOTS=()

SSH_OPTS=(
  -o BatchMode=yes
  -o ConnectTimeout=20
  -o StrictHostKeyChecking=yes
)

DEFAULT_ROOTS=(
  /usr/local/tomcat/content/erddap
  /opt/tomcat/content/erddap
  /opt/erddap
  /usr/local/erddap
  /var/erddap
  /srv/erddap
)

usage() {
  cat <<EOF
Usage: $0 [--host HOST --user USER] [--local DIR] [options]

SSH into an ERDDAP server (or scan a local tree) and report or rewrite
"${OLD_DEFAULT}" → "${NEW_DEFAULT}".

Modes
  (default)     Read-only scan: print file:line matches
  --apply       Rewrite matches in place after writing a .bak backup

Connection (one of)
  --host HOST   SSH hostname (required unless --local)
  --user USER   SSH user (default: \$USER or erddap)
  --port PORT   SSH port (default: 22)
  --identity F  SSH private key
  --local DIR   Operate on a local directory instead of SSH

Search
  --root DIR    Search root (repeatable). Default: common ERDDAP content paths.
                With --local and no --root, the local directory itself is used.
  --old TEXT    Phrase to find (default: ${OLD_DEFAULT})
  --new TEXT    Replacement (default: ${NEW_DEFAULT})

Safety
  Read-only is the default. Host-key checking stays on (StrictHostKeyChecking=yes).
  Only use this on hosts you already administer.
EOF
}

die() { echo "error: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --host) HOST="${2:?}"; shift 2 ;;
    --user) USER_NAME="${2:?}"; shift 2 ;;
    --port) PORT="${2:?}"; shift 2 ;;
    --identity|--key) IDENTITY="${2:?}"; shift 2 ;;
    --local) LOCAL_DIR="${2:?}"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --dry-run|--read-only|--check) APPLY=0; shift ;;
    --root) ROOTS+=("${2:?}"); shift 2 ;;
    --old) OLD="${2:?}"; shift 2 ;;
    --new) NEW="${2:?}"; shift 2 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[[ -n "$OLD" && -n "$NEW" ]] || die "--old and --new must be non-empty"
[[ "$OLD" != "$NEW" ]] || die "--old and --new are identical"

if [[ -n "$LOCAL_DIR" ]]; then
  [[ -d "$LOCAL_DIR" ]] || die "--local is not a directory: $LOCAL_DIR"
  LOCAL_DIR="$(cd "$LOCAL_DIR" && pwd)"
elif [[ -z "$HOST" ]]; then
  die "provide --host HOST or --local DIR"
fi

if [[ -n "$IDENTITY" ]]; then
  [[ -f "$IDENTITY" ]] || die "identity file not found: $IDENTITY"
  SSH_OPTS+=(-i "$IDENTITY" -o IdentitiesOnly=yes)
fi

if ((${#ROOTS[@]} == 0)); then
  if [[ -n "$LOCAL_DIR" ]]; then
    ROOTS=("$LOCAL_DIR")
  else
    ROOTS=("${DEFAULT_ROOTS[@]}")
  fi
fi

read -r -d '' WORKER <<'PY' || true
import datetime, re, shutil, sys
from pathlib import Path

old, new, apply_s = sys.argv[1], sys.argv[2], sys.argv[3]
roots = sys.argv[4:]
apply = apply_s == "1"

exts = {".xml", ".properties", ".csv", ".tsv", ".json", ".ncml", ".md",
        ".txt", ".html", ".cfg", ".conf", ".yml", ".yaml"}

def is_text(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:8000]
    except OSError:
        return False
    return b"\x00" not in chunk

files = []
roots_found = []
for r in roots:
    p = Path(r)
    if not p.is_dir():
        continue
    roots_found.append(str(p))
    for f in p.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in exts and f.name != "datasets.xml":
            continue
        if ".bak." in f.name or f.suffix == ".bak":
            continue
        if ".git" in f.parts:
            continue
        if is_text(f):
            files.append(f)

pat = re.compile(re.escape(old), re.IGNORECASE)

def repl(m):
    s = m.group(0)
    if s.isupper():
        return new.upper()
    if s.islower():
        return new.lower()
    # Title or mixed case: use --new as provided ("Lake of America").
    return new

hits = []
files_hit = 0
occurrences = 0
changed = []

for f in sorted(files):
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    found = list(pat.finditer(text))
    if not found:
        continue
    files_hit += 1
    occurrences += len(found)
    for i, line in enumerate(text.splitlines(), 1):
        if pat.search(line):
            hits.append("%s:%d:%s" % (f, i, line))
    if apply:
        stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        bak = f.with_name(f.name + ".bak." + stamp)
        shutil.copy2(f, bak)
        f.write_text(pat.sub(repl, text), encoding="utf-8")
        changed.append(str(f))

print("mode: %s" % ("apply" if apply else "read-only"))
print("roots scanned:")
if roots_found:
    for r in roots_found:
        print("  %s" % r)
else:
    print("  (none existed)")
print("files with matches: %d" % files_hit)
print("occurrences: %d" % occurrences)
print("--- matches ---")
print("\n".join(hits) if hits else "(none)")
print("---")
if apply:
    print("apply complete (%d file(s)); backups are *.bak.<UTC>" % len(changed))
else:
    print("read-only: no files were modified. Re-run with --apply to rewrite.")

if not roots_found:
    sys.exit(1)
PY

if [[ -n "$LOCAL_DIR" ]]; then
  echo "target: local $LOCAL_DIR"
  python3 -c "$WORKER" "$OLD" "$NEW" "$APPLY" "${ROOTS[@]}"
else
  echo "target: ${USER_NAME}@${HOST}:${PORT}"
  b64=$(printf '%s' "$WORKER" | base64 | tr -d '\n')
  root_args=""
  for r in "${ROOTS[@]}"; do
    root_args+=" $(printf '%q' "$r")"
  done
  ssh "${SSH_OPTS[@]}" -p "$PORT" "${USER_NAME}@${HOST}" \
    "echo $b64 | base64 -d | python3 - $(printf '%q' "$OLD") $(printf '%q' "$NEW") $(printf '%q' "$APPLY")$root_args"
fi
