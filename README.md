# ERDDAP administrator rename utility

Scan an ERDDAP installation's dataset metadata, review a saved change plan, then explicitly apply it. Administrators run the utility **on the server after connecting with their usual SSH client**. It needs Python 3.9+ on Linux or macOS, uses the standard library, and requires no server access from the tool's authors.

Names are administrator-supplied configuration. `Lake Ontario` → `Lake of America` below is the requested example, not a statement about an official geographic designation. Use different `--old` and `--new` values for future changes.

## Administrator workflow

Copy this repository to the server and change into its directory. Replace the example paths with this installation's actual paths. Keep change artifacts outside web-served directories.

```bash
umask 077
mkdir -p /secure/change

# Read local dataset configuration; write a plan and readable report.
python3 erddap_admin_rename.py scan \
  --datasets /actual/content/erddap/datasets.xml \
  --old 'Lake Ontario' --new 'Lake of America' \
  --plan /secure/change/plan.json

# Review every proposed edit and note the full SHA256 printed here.
python3 erddap_admin_rename.py review --plan /secure/change/plan.json

# Replace FULL_PLAN_SHA256 with that reviewed digest.
python3 erddap_admin_rename.py apply \
  --plan /secure/change/plan.json \
  --confirm FULL_PLAN_SHA256 \
  --backup-dir /secure/change/backups
```

The scan writes private `plan.json` and `plan.json.report.txt` artifacts; it does not change dataset configuration. Omit `--plan` for a report to standard output only. Saved artifact names must be new: scans do not overwrite existing plans or reports.

The report identifies planned edits and findings requiring manual work. Apply checks that the inputs still match the reviewed plan, backs up the originals in a unique transaction directory, and writes a transaction manifest. **Apply does not reload ERDDAP.**

To request reloads after reviewing the result, use the manifest path printed by apply:

```bash
python3 erddap_admin_rename.py review --manifest /secure/change/backups/TRANSACTION/manifest.json
python3 erddap_admin_rename.py reload \
  --manifest /secure/change/backups/TRANSACTION/manifest.json \
  --big-parent /actual/erddapData \
  --confirm FULL_MANIFEST_SHA256
```

This creates ordinary dataset reload flags. Check ERDDAP's logs and public metadata afterward; queued flags are not proof that a reload succeeded. The [administrator runbook](docs/administrator-runbook.md) covers rehearsal, verification, rollback, permissions, and limits.

## What is covered

- Existing textual `<att>` values inside dataset and variable `<addAttributes>` blocks. Default attribute names: `title`, `summary`, `keywords`, `long_name`, `comment`, `description`, `acknowledgement`, `acknowledgment`, `institution`, and `project`.
- Additional text attributes selected with repeatable `--attribute NAME`; case-sensitive matching selected with `--case-sensitive`. Default matching ignores case and preserves all-uppercase or all-lowercase spelling; other matches use `--new` as supplied.
- Multiple explicit configuration inputs using repeated `--datasets PATH`, nested datasets, and relative local XML XIncludes within each selected configuration directory. Inputs must be UTF-8 XML 1.0 regular files without symlinks or hard links. The report attributes shared-fragment changes to affected datasets; unsupported include modes and XML constructs are rejected.
- An optional public metadata audit with `--server https://your-server.example/erddap`. It performs HTTP reads and reports inherited/source-only matches for manual action; it does not insert new metadata overrides or modify the remote server.

The utility does not automatically change source NetCDF files, database records, dataset IDs, variable names, URLs, other configuration files, prebuilt FGDC/ISO metadata, or labels baked into images and external maps. Public inventory covers currently visible datasets, not a complete inventory of private, inactive, or unloaded datasets. A rejected or incomplete scan is not a clean inventory.

ERDDAP normally combines source metadata with administrator `addAttributes`. `EDDGridFromErddap` and `EDDTableFromErddap` do not support local attribute overrides and require changes at their source. See the official [metadata guidance](https://erddap.github.io/docs/server-admin/datasets#addattributes), [remote dataset restrictions](https://erddap.github.io/docs/server-admin/datasets#no-addattributes-axisvariable-or-datavariable), and [normal reload flags](https://erddap.github.io/docs/server-admin/additional-information#flag).

## Other entry points and validation

`python3 erddap_admin.py` and `bash erddap_ssh_rename.sh` forward to the same `scan`, `review`, `apply`, `rollback`, and `reload` commands. The former `--host`, `--pull`, `--root`, and `--apply` interfaces are replaced by this workflow. SSH into the server first; these entry points do not construct remote shell commands.

`erddap_search.py` searches public catalogs. `erddap_rename.py` modifies downloaded copies; it is separate from the server administrator workflow and cannot update the upstream catalog.

```bash
python3 erddap_admin_rename.py --help
python3 -m unittest discover -s tests -v
```

Validation in this repository uses local fixtures and automated tests. The authors have no administrator access to NOAA's servers, and this utility has not been validated against their production installation. Rehearse on copied files, then verify the actual installation using the runbook.

Files with native macOS ACLs or extended attributes are refused to prevent that metadata from being lost. Schedule a maintenance window for changes spanning multiple files; replacement is atomic per file, and the utility does not pause ERDDAP's automatic reloads.
