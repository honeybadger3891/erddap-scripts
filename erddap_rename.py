#!/usr/bin/env python3
"""Download an ERDDAP dataset and rename a lake in its local copies.

Remote ERDDAP servers (NOAA, GLoS, etc.) are read-only — this renames the
lake in the data you download:
  - CSV:  "#"-comment metadata header (and data rows with --all)
  - netCDF: global + variable attributes (requires netCDF4)
  - ISO 19115 XML metadata: all occurrences (--meta)

Usage:
    python3 erddap_rename.py --server glerl --id LO_CHL_NRT
    python3 erddap_rename.py --server glos --id glisa_general_annual_ontario --format csv
    python3 erddap_rename.py --server glerl --id LO_SST_FP_o1 --meta --out ./renamed
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

SERVERS = {
    "glerl": "https://apps.glerl.noaa.gov/erddap",
    "glos": "https://seagull-erddap.glos.org/erddap",
}

OLD_LAKE = "Lake Ontario"
NEW_LAKE = "Lake of America"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "erddap-scripts/0.1"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def rename_csv(data: bytes, old_lake: str, new_lake: str, all_bytes: bool):
    """Replace lake name in ERDDAP CSV '#'-comment header; --all hits data rows too."""
    text = data.decode("utf-8", "replace")
    lines = text.splitlines(keepends=True)
    renamed = 0
    for i, line in enumerate(lines):
        if line.startswith("#"):
            if old_lake in line:
                lines[i] = line.replace(old_lake, new_lake)
                renamed += line.count(old_lake)
        elif all_bytes and old_lake in line:
            lines[i] = line.replace(old_lake, new_lake)
            renamed += line.count(old_lake)
    return "".join(lines).encode("utf-8"), renamed


def rename_nc(data: bytes, old_lake: str, new_lake: str):
    """Rename in netCDF global and variable attributes (requires netCDF4)."""
    try:
        import netCDF4
    except ImportError:
        print("netCDF4 not installed — pip install netCDF4", file=sys.stderr)
        return data, 0
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as f:
        tmp = f.name
    with open(tmp, "wb") as f:
        f.write(data)
    ds = netCDF4.Dataset(tmp, "a")
    renamed = 0
    attrs = [(ds, a) for a in ds.ncattrs()]
    for var in ds.variables.values():
        attrs += [(var, a) for a in var.ncattrs()]
    for obj, attr in attrs:
        val = getattr(obj, attr)
        if isinstance(val, str) and old_lake in val:
            setattr(obj, attr, val.replace(old_lake, new_lake))
            renamed += val.count(old_lake)
        elif isinstance(val, bytes) and old_lake.encode() in val:
            setattr(obj, attr, val.decode().replace(old_lake, new_lake))
            renamed += val.decode().count(old_lake)
    ds.close()
    with open(tmp, "rb") as f:
        out = f.read()
    os.unlink(tmp)
    return out, renamed


def rename_text(data: bytes, old_lake: str, new_lake: str):
    out = data.replace(old_lake.encode(), new_lake.encode())
    return out, data.count(old_lake.encode())


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", required=True, help="server name (glerl, glos) or base URL")
    ap.add_argument("--id", required=True, dest="dsid", help="dataset ID, e.g. LO_CHL_NRT")
    ap.add_argument("--format", choices=["csv", "nc", "auto"], default="auto")
    ap.add_argument("--meta", action="store_true",
                    help="also download+rename the ISO 19115 metadata XML")
    ap.add_argument("--old", default=OLD_LAKE)
    ap.add_argument("--new", default=NEW_LAKE)
    ap.add_argument("--all", action="store_true",
                    help="for CSV: also replace in data rows, not just metadata header")
    ap.add_argument("--out", default=".", help="output directory")
    args = ap.parse_args()

    old_lake, new_lake = args.old, args.new
    base = (SERVERS.get(args.server) or args.server).rstrip("/")

    # --- download ---
    csv_url = f"{base}/tabledap/{args.dsid}.csv"
    nc_url = f"{base}/griddap/{args.dsid}.nc"
    fmt = args.format
    if fmt == "auto":
        try:
            data, fmt = fetch(csv_url), "csv"
        except urllib.error.HTTPError:
            data, fmt = fetch(nc_url), "nc"
    elif fmt == "csv":
        fmt, data = "csv", fetch(csv_url)
    else:
        fmt, data = "nc", fetch(nc_url)
    print(f"Downloaded {args.dsid} as {fmt}: {len(data)/1e6:.1f} MB")

    # --- rename ---
    if fmt == "csv":
        out, renamed = rename_csv(data, old_lake, new_lake, args.all)
        ext = ".csv"
    elif fmt == "nc":
        out, renamed = rename_nc(data, old_lake, new_lake)
        ext = ".nc"
    else:
        out, renamed = rename_text(data, old_lake, new_lake)
        ext = ".csv"

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"{args.dsid}{ext}")
    with open(path, "wb") as f:
        f.write(out)
    print(f"Wrote {path} — {renamed} replacement(s) in data file")

    # --- ISO 19115 metadata ---
    if args.meta:
        for suffix, label in [("_iso19115.xml", "ISO"), ("_fgdc.xml", "FGDC")]:
            url = f"{base}/metadata/iso19115/xml/{args.dsid}{suffix}"
            if label == "FGDC":
                url = f"{base}/metadata/fgdc/xml/{args.dsid}{suffix}"
            try:
                mdata = fetch(url)
            except urllib.error.HTTPError:
                continue
            mout, mren = rename_text(mdata, old_lake, new_lake)
            mpath = os.path.join(args.out, f"{args.dsid}{suffix}")
            with open(mpath, "wb") as f:
                f.write(mout)
            print(f"Wrote {mpath} — {mren} replacement(s) in {label} metadata")

    summary = {"id": args.dsid, "server": base, "format": fmt,
               "data_renamed": renamed}
    if args.meta:
        summary["meta"] = os.path.exists(os.path.join(
            args.out, f"{args.dsid}_iso19115.xml"))
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
