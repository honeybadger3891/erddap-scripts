#!/usr/bin/env python3
"""Build the portable administrator utility using only the standard library."""

import argparse
import hashlib
import io
from pathlib import Path
import sys
import zipfile


RUNTIME_FILES = (
    "erddapctl.py",
    "erddap_site.py",
    "erddap_admin_rename.py",
    "erddap_metadata.py",
    "erddap_catalog.py",
)
DOCUMENT_FILES = ("README.md", "docs/administrator-runbook.md")
ENTRY_POINT = b"from erddapctl import main\n\nraise SystemExit(main())\n"


def build_bundle(output, source_root=None):
    """Create a deterministic zipapp at a new path and return its SHA256."""
    root = Path(source_root) if source_root is not None else Path(__file__).resolve().parents[1]
    entries = {"__main__.py": ENTRY_POINT}
    for name in RUNTIME_FILES + DOCUMENT_FILES:
        source = root / name
        if source.is_symlink() or not source.is_file():
            raise ValueError("Required bundle source is not a regular file: %s" % source)
        data = source.read_bytes()
        if name.endswith(".py"):
            compile(data, name, "exec")
        entries[name] = data

    archive = io.BytesIO()
    archive.write(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_STORED) as bundle:
        for name in sorted(entries):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            bundle.writestr(entry, entries[name])
    data = archive.getvalue()
    # Validate all sources before creating the output. Exclusive creation also
    # refuses an existing output symlink, preserving an earlier distribution.
    with Path(output).open("xb") as destination:
        destination.write(data)
    return hashlib.sha256(data).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build one portable ERDDAP administrator .pyz file. No installation or network access is needed."
    )
    parser.add_argument("--output", required=True, type=Path, help="New output path; existing files are never overwritten.")
    args = parser.parse_args(argv)
    try:
        digest = build_bundle(args.output)
    except (OSError, ValueError, SyntaxError) as error:
        print("Bundle build failed: %s" % error, file=sys.stderr)
        return 1
    print("Created: %s" % args.output)
    print("SHA256: %s" % digest)
    print("Run on the administrator's server: python3 erddapctl.pyz --help")
    return 0


if __name__ == "__main__":
    sys.exit(main())
