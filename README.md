# ERDDAP Scripts

Scripts for identifying and processing datasets on [ERDDAP](https://coastwatch.pfeg.noaa.gov/erddap/) servers, focused on the Laurentian Great Lakes catalog (Lake Ontario → Lake of America renaming).

## Servers

| Name | Base URL | Contents |
|------|----------|----------|
| `glerl` | `https://apps.glerl.noaa.gov/erddap` | NOAA Great Lakes Research Center — 58 Lake Ontario datasets (satellite: SST, chlorophyll, true color, winds, ice; time series: water level) |
| `glos` | `https://seagull-erddap.glos.org/erddap` | GLoS — Great Lakes in Situ Archive (GLISA), moorings/thermistors/ADCP, 32 Ontario datasets |

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

Python 3.9+ (stdlib only for search/CSV/ISO). `netCDF4` only for netCDF attribute renaming.
