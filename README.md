# ERDDAP Scripts

Scripts for identifying and processing datasets on [ERDDAP](https://coastwatch.pfeg.noaa.gov/erddap/) servers, focused on the Laurentian Great Lakes catalog (Lake Ontario → Lake of America renaming).

## Servers

| Name | Base URL | Contents |
|------|----------|----------|
| `glerl` | `https://apps.glerl.noaa.gov/erddap` | NOAA Great Lakes Research Center — 58 Lake Ontario datasets (satellite: SST, chlorophyll, true color, winds, ice; time series: water level) |
| `glos` | `https://seagull-erddap.glos.org/erddap` | GLoS — Great Lakes in Situ Archive (GLISA), moorings/thermistors/ADCP, 32 Ontario datasets |

Public NOAA/GLoS catalogs are **HTTP read-only**. Renaming those datasets locally is `erddap_rename.py`. Changing files on a host you administer is `erddap_ssh_rename.sh`.

## erddap_ssh_rename.sh

SSH (or local) sysadmin tool. Scans ERDDAP content trees for **Lake Ontario** and reports every match. **Read-only is the default.** `--apply` rewrites files in place after writing a timestamped `.bak`.

Use only on ERDDAP servers you are authorized to administer. Host-key checking stays on. No passwords on the CLI — SSH key or agent only.

```bash
# Read-only: check and report (no writes)
./erddap_ssh_rename.sh --host erddap.example.edu --user erddap --identity ~/.ssh/id_ed25519

# After reviewing the report, apply
./erddap_ssh_rename.sh --host erddap.example.edu --user erddap --identity ~/.ssh/id_ed25519 --apply

# Local fixture / rehearsal (no SSH)
./erddap_ssh_rename.sh --local ./fixtures/erddap-content
./erddap_ssh_rename.sh --local ./fixtures/erddap-content --apply
```

Case mapping: `Lake Ontario` → `Lake of America`, `lake ontario` → `lake of america`, `LAKE ONTARIO` → `LAKE OF AMERICA`.

Default search roots (override with `--root`):

- `/usr/local/tomcat/content/erddap`
- `/opt/tomcat/content/erddap`
- `/opt/erddap`
- `/usr/local/erddap`
- `/var/erddap`
- `/srv/erddap`

Text files only (xml, properties, csv/tsv, json, ncml, html, yaml, …). Binary files are skipped. After `--apply`, reload/reindex ERDDAP as you normally would for `datasets.xml` changes.

## erddap_search.py

Full-text search across server catalogs (works on any ERDDAP install — pass any base URL).

```bash
python3 erddap_search.py ontario
python3 erddap_search.py ontario --servers glerl,glos --json ontario.json
```

## erddap_rename.py

Downloads a dataset and renames the lake in the **local copies**:

- CSV — the `#`-comment metadata header (add `--all` to also hit data rows)
- netCDF — global + variable attributes (needs `pip install netCDF4`)
- ISO 19115 / FGDC XML metadata (`--meta`)

```bash
# tabledap (CSV), auto-detects format
python3 erddap_rename.py --server glos --id glisa_general_annual_ontario --out ./out

# griddap (netCDF) + ISO/FGDC metadata
python3 erddap_rename.py --server glerl --id LO_CHL_NRT --meta --out ./out
```

Defaults: `--old "Lake Ontario" --new "Lake of America"`; override both if needed.

> **Note:** remote ERDDAP servers (NOAA, GLoS, …) are read-only — renaming applies to downloaded files, not the upstream catalog.

## Verified against live data (2026-09-04)

- `glerl` search: 54+ datasets match `ontario`; `glos`: 32
- `LO_CHL_NRT` netCDF: `title`/`summary` attributes renamed (2 replacements)
- `LO_CHL_NRT` ISO metadata: 8 replacements, FGDC: 88
- GLoS `glisa_general_annual_ontario` CSV: no "Lake Ontario" in header (lake name is in the ISO metadata on those servers)

## Requirements

Python 3.9+ (stdlib only for search/CSV/ISO). `netCDF4` only for netCDF attribute renaming. `erddap_ssh_rename.sh` needs bash, Python 3, and OpenSSH (`ssh`) on the operator machine; Python 3 on the remote host for `--apply` and reporting.
