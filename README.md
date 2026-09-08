# ERDDAP administrator rename utility

Scan an ERDDAP installation's dataset metadata, review a saved change plan, then explicitly apply it. Administrators run the utility **on the server after connecting with their usual SSH client**. It needs Python 3.9+ on Linux or macOS, uses the standard library, and requires no server access from the tool's authors.

Names are administrator-supplied configuration. `Lake Ontario` → `Lake of America` below is the requested example, not a statement about an official geographic designation. Use different `--old` and `--new` values for future changes.

## Administrator quickstart

Copy the single **`erddapctl.pyz`** distribution file to the server using your usual file-transfer method. No Git, pip install, container, or service installation is required there. Connect through SSH and run these commands from the file's directory, replacing `/secure/erddap-admin` with a private location outside web-served directories:

```bash
umask 077
mkdir -p /secure/erddap-admin

# Once per installation: answer prompts for the active configuration and paths.
python3 erddapctl.pyz setup --profile /secure/erddap-admin/site.json

# For this change and future changes: use the terminal menu.
python3 erddapctl.pyz menu --profile /secure/erddap-admin/site.json
```

The menu can scan a name change, show history, open a saved change, or check setup. It asks for the old and new names, displays every proposed before/after value, and requires the displayed confirmation phrase before applying. **Pressing Enter cancels.** Plans, readable reports, backups, and transaction records are kept together in a unique change directory. Apply checks that the configuration still matches the reviewed plan. Reload and rollback are separate, confirmed actions available by opening a saved change.

For a saved report with no update prompt:

```bash
python3 erddapctl.pyz rename --profile /secure/erddap-admin/site.json \
  --old 'Lake Ontario' --new 'Lake of America' --scan-only
python3 erddapctl.pyz history --profile /secure/erddap-admin/site.json
```

Future renames use the same profile with different names. `doctor --profile PATH` checks local setup without changing files or contacting ERDDAP. Add `--audit-public` to `rename` to supplement the local scan using the optional server URL saved in the profile; this can take longer and performs HTTP reads. Default scans are local.

Read the [administrator runbook](docs/administrator-runbook.md) before the first change. It covers the actual installation paths, rehearsal, confirmation, verification, rollback, and upgrades. Queued reload flags are not proof of successful loading; check ERDDAP's logs and public metadata afterward.

## Build and distribute

From a source checkout, a maintainer can build the distribution using only Python:

```bash
python3 -m unittest discover -s tests -v
python3 scripts/build_admin_bundle.py --output erddapctl.pyz
python3 erddapctl.pyz --help
```

The builder prints a SHA256 checksum and refuses to overwrite an existing output. Distribute the resulting file, its checksum, and the runbook through your normal administrative channel. The archive contains only the administrator runtime and documentation; it contains no site settings, credentials, tests, or fixtures. Identical source files produce identical archive bytes. Keep site profiles and change history outside the code distribution so a new utility file can be tested and copied into place without replacing those records.

## What is covered

- Existing textual `<att>` values inside dataset and variable `<addAttributes>` blocks. Default attribute names: `title`, `summary`, `keywords`, `long_name`, `comment`, `description`, `acknowledgement`, `acknowledgment`, `institution`, and `project`.
- Additional text attributes selected with repeatable `--attribute NAME`; case-sensitive matching selected with `--case-sensitive`. Default matching ignores case and preserves all-uppercase or all-lowercase spelling; other matches use `--new` as supplied.
- Multiple explicit configuration inputs using repeated `--datasets PATH`, nested datasets, and relative local XML XIncludes within each selected configuration directory. Inputs must be UTF-8 XML 1.0 regular files without symlinks or hard links. The report attributes shared-fragment changes to affected datasets; unsupported include modes and XML constructs are rejected.
- An optional public metadata audit with `--server https://your-server.example/erddap`. It performs HTTP reads and reports inherited/source-only matches for manual action; it does not insert new metadata overrides or modify the remote server.

The utility does not automatically change source NetCDF files, database records, dataset IDs, variable names, URLs, other configuration files, prebuilt FGDC/ISO metadata, or labels baked into images and external maps. Public inventory covers currently visible datasets, not a complete inventory of private, inactive, or unloaded datasets. A rejected or incomplete scan is not a clean inventory.

ERDDAP normally combines source metadata with administrator `addAttributes`. `EDDGridFromErddap` and `EDDTableFromErddap` do not support local attribute overrides and require changes at their source. See the official [metadata guidance](https://erddap.github.io/docs/server-admin/datasets#addattributes), [remote dataset restrictions](https://erddap.github.io/docs/server-admin/datasets#no-addattributes-axisvariable-or-datavariable), and [normal reload flags](https://erddap.github.io/docs/server-admin/additional-information#flag).

## Other entry points and validation

`python3 erddapctl.py` provides the same guided commands from a source checkout. `python3 erddapctl.pyz advanced COMMAND ...` exposes the original `scan`, `review`, `apply`, `rollback`, and `reload` commands, including full SHA256 confirmations for deliberate automation. See the runbook for examples. Interactive apply, reload, and rollback require a terminal; redirected input cannot silently approve them.

`python3 erddap_admin.py` and `bash erddap_ssh_rename.sh` forward to the same `scan`, `review`, `apply`, `rollback`, and `reload` commands. The former `--host`, `--pull`, `--root`, and `--apply` interfaces are replaced by this workflow. SSH into the server first; these entry points do not construct remote shell commands.

`erddap_search.py` searches public catalogs. `erddap_rename.py` modifies downloaded copies; it is separate from the server administrator workflow and cannot update the upstream catalog.

```bash
python3 erddap_admin_rename.py --help
python3 -m unittest discover -s tests -v
```

Validation in this repository uses local fixtures and automated tests. The authors have no administrator access to NOAA's servers, and this utility has not been validated against their production installation. Rehearse on copied files, then verify the actual installation using the runbook.

Files with native macOS ACLs or extended attributes are refused to prevent that metadata from being lost. Schedule a maintenance window for changes spanning multiple files; replacement is atomic per file, and the utility does not pause ERDDAP's automatic reloads.
