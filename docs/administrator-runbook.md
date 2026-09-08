# Administrator runbook

This procedure is for a conventional ERDDAP installation administered through SSH. Run the commands on that server using an account authorized to edit its dataset configuration. Python 3.9+ on Linux or macOS is required; the utility uses the standard library, including POSIX `fcntl` locking. No remote credentials or SSH automation are built in.

## Guided everyday workflow

Use the single `erddapctl.pyz` file supplied by the maintainer. The server needs no Git checkout, pip packages, container, or new running service. First identify the active paths and permissions described in section 1 below. Then save this installation's settings once:

```bash
umask 077
mkdir -p /secure/erddap-admin
python3 erddapctl.pyz setup --profile /secure/erddap-admin/site.json
```

Setup asks for the active `datasets.xml`, a private directory for change history and backups, and optional `bigParentDirectory` and public ERDDAP URL. It does not edit dataset configuration. Alternatively, supply the paths explicitly:

```bash
python3 erddapctl.pyz setup --profile /secure/erddap-admin/site.json \
  --datasets /actual/content/erddap/datasets.xml \
  --workspace /secure/erddap-admin/changes \
  --big-parent /actual/erddapData
```

Use only one of those setup commands: an existing profile is not overwritten. Repeat `--datasets` when several independently managed configuration files belong to this installation. `--server https://your-server.example/erddap` saves an optional URL for public audits. A missing reload path or public URL is a warning and does not prevent a local scan; correct reported setup errors before proceeding. Use a different profile and workspace for each installation, and a separate pair pointing only to copied configuration for rehearsal.

Open the menu for each maintenance session:

```bash
python3 erddapctl.pyz menu --profile /secure/erddap-admin/site.json
```

Choose **Scan a name change** and enter the current and replacement names. The utility creates a unique change directory, saves the plan and text report, and shows the proposed edits and manual findings. Review all before/after values. To update, type the exact displayed phrase, such as `APPLY` followed by the displayed plan identifier. Press Enter to cancel; the saved plan remains available. Apply still verifies the full saved plan and current file contents, creates backups, and refuses stale inputs.

The menu also lists change history and opens a saved change by its displayed number or ID. A saved change contains its own site-setting snapshot, plan, readable report, scan log (including error details), and any transaction backups. Opening it allows review, apply, reload, or rollback. Reload and rollback show the affected files or dataset IDs and require their own confirmation. Apply and rollback never automatically reload ERDDAP. Check logs and served metadata as described in section 5 after requesting reloads; a history status of `applied` describes the file transaction, not successful loading by ERDDAP.

Direct commands are available when more convenient:

```bash
# Save an inventory for review without an apply prompt.
python3 erddapctl.pyz rename --profile /secure/erddap-admin/site.json \
  --old 'Lake Ontario' --new 'Lake of America' --scan-only

python3 erddapctl.pyz history --profile /secure/erddap-admin/site.json
python3 erddapctl.pyz review --job /secure/erddap-admin/changes/CHANGE_ID
python3 erddapctl.pyz apply --job /secure/erddap-admin/changes/CHANGE_ID
python3 erddapctl.pyz reload --job /secure/erddap-admin/changes/CHANGE_ID

# When a restoration is needed, review and confirm it separately.
python3 erddapctl.pyz rollback --job /secure/erddap-admin/changes/CHANGE_ID
```

Replace `CHANGE_ID` with the ID in history or use the full change-directory path printed by the scan. For future renames, run `rename` with different names or return to the menu. Add `--audit-public` to `rename` when a public URL is saved and a public metadata audit is wanted; normal menu scans inspect local configuration. An incomplete audit leaves its evidence in history and blocks guided apply. A scan with no eligible changes does not produce an apply prompt; inspect any manual findings in the saved report.

`python3 erddapctl.pyz doctor --profile /secure/erddap-admin/site.json` checks the saved local configuration, paths, and permissions without changing files or contacting ERDDAP. It does not prove that source datasets will load. Guided confirmations require a real terminal. To use the explicit digest-confirmed interface for a reviewed automation, prefix its command with `python3 erddapctl.pyz advanced`; for example, `python3 erddapctl.pyz advanced review --plan PATH`. The detailed procedures below use the equivalent source-checkout command `python3 erddap_admin_rename.py`.

## Utility upgrades and retained records

Keep the `.pyz` file separate from the profile and change directories. Compare its SHA256 with the checksum supplied by the maintainer after copying it; use `sha256sum erddapctl.pyz` on Linux or `shasum -a 256 erddapctl.pyz` on macOS. Obtain the checksum through your normal trusted distribution channel. `python3 erddapctl.pyz --help` checks that the archive starts with the server's Python; `--version` prints the utility version.

For an upgrade, copy the new file to a different filename or directory, verify its checksum, run `--help` and `doctor --profile PATH`, and rehearse against copied configuration before using it for production work. Keep the prior utility file until the new version is accepted. Profiles and change records remain at their existing paths; do not place them in a directory that gets replaced during deployment. If installation settings change, create a new profile filename and review the values. Earlier changes retain their original settings snapshot, including the reload directory, so inspect those settings before resuming an older change.

Retain each entire change directory, including its original file backups and manifest, according to your organization's change-record policy. Deleting the directory removes the utility's rollback material. History is local to the saved workspace; this utility provides no central dashboard, scheduled service, or automatic retention policy.

## 1. Identify this installation

Locate the active `datasets.xml`, conventionally under `tomcat/content/erddap/`. Confirm how it is maintained: if another process generates it, preserve the rename in that process or the next generation may undo it. Prevent concurrent configuration edits during apply and rollback.

Find the running installation's `bigParentDirectory`. `setup.xml` usually specifies it, but an `ERDDAP_bigParentDirectory` environment override may change the effective value. Do not guess a flag directory from the Tomcat installation path. See the official [installation configuration](https://erddap.github.io/docs/server-admin/deploy-install#setupxml) and [environment overrides](https://erddap.github.io/docs/server-admin/deploy-install#environment-variables).

Prepare a private change directory outside ERDDAP's web content and source-data directories. Use `umask 077`. Plans, reports, and backups can contain nonpublic metadata; retain them as administrator records. Artifacts use mode `0600` and newly created artifact directories are private. Protect existing parent directories as well. The account needs permission to preserve the configuration files' ownership and mode when applying replacements.

File replacement preserves POSIX ownership/mode and Linux extended attributes, including access ACLs. Files with native macOS ACLs or extended attributes are refused because this utility cannot reliably preserve that metadata on macOS; use a metadata-preserving administration workflow for them. For a plan spanning several files, schedule a configuration maintenance window: each file is replaced atomically, but the entire group is not a single filesystem transaction. ERDDAP's automatic reloads can otherwise observe files between replacements. The utility's locks coordinate its own writers; they do not pause ERDDAP or configuration generators.

## 2. Rehearse on copies

From the repository root, run the tests and use a temporary copy of the fixture. The fixture exercises XML editing; it is not a complete production dataset with accessible source data.

```bash
python3 -m unittest discover -s tests -v
umask 077
rename_rehearsal=$(mktemp -d "${TMPDIR:-/tmp}/erddap-rename.XXXXXX")
rename_rehearsal=$(python3 -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).resolve())' "$rename_rehearsal")
cp -R fixtures/erddap-content "$rename_rehearsal/content"
cp "$rename_rehearsal/content/datasets.xml" "$rename_rehearsal/original.xml"

python3 erddap_admin_rename.py scan \
  --datasets "$rename_rehearsal/content/datasets.xml" \
  --old 'Lake Ontario' --new 'Lake of America' \
  --plan "$rename_rehearsal/plan.json"
python3 erddap_admin_rename.py review --plan "$rename_rehearsal/plan.json"
```

Read all edits before copying the full digest from `review`:

```bash
printf 'Paste the reviewed plan SHA256: '
IFS= read -r rename_plan_sha
python3 erddap_admin_rename.py apply \
  --plan "$rename_rehearsal/plan.json" \
  --confirm "$rename_plan_sha" \
  --backup-dir "$rename_rehearsal/backups"
diff -u "$rename_rehearsal/original.xml" "$rename_rehearsal/content/datasets.xml"
```

`diff` exits with status 1 when it finds the expected changes. Inspect them. Apply prints the manifest path; use it to rehearse rollback:

```bash
printf 'Paste the transaction manifest path printed by apply: '
IFS= read -r rename_manifest
python3 erddap_admin_rename.py review --manifest "$rename_manifest"
printf 'Paste the reviewed manifest SHA256: '
IFS= read -r rename_manifest_sha
python3 erddap_admin_rename.py rollback \
  --manifest "$rename_manifest" --confirm "$rename_manifest_sha"
cmp "$rename_rehearsal/original.xml" "$rename_rehearsal/content/datasets.xml"
```

`cmp` should exit successfully with no output. These commands do not touch an ERDDAP server or its reload flags. Repeat the rehearsal with copies of the actual configuration before deploying changes. Preserve the copied files' relationships if the configuration has external fragments.

The path-resolution step accommodates systems where `/tmp` or a parent of `TMPDIR` is a symlink. Configuration and artifact paths must use real directories; the tool refuses symlinks rather than replacing their targets implicitly.

## 3. Build and inspect the real plan

```bash
umask 077
mkdir -p /secure/change
python3 erddap_admin_rename.py scan \
  --datasets /actual/content/erddap/datasets.xml \
  --old 'Lake Ontario' --new 'Lake of America' \
  --plan /secure/change/plan.json
python3 erddap_admin_rename.py review --plan /secure/change/plan.json
```

Omit `--plan` for a report to standard output without saved artifacts. Repeat `--datasets` for additional independently managed configuration files. The scanner processes the specified inputs and supported local XML XIncludes; it is not a filesystem-wide search. It handles nested datasets and tracks affected datasets through shared fragments. Unsupported XML/include constructs cause a refusal: resolve the reported configuration issue rather than skipping that input. ERDDAP's [XInclude support](https://erddap.github.io/docs/server-admin/datasets#xinclude) can combine datasets or reused definitions; it must be enabled appropriately in the running installation.

Supported inputs are UTF-8 XML 1.0 regular files with one hard link and no symlinks in their paths. XInclude paths must be relative, remain within the selected root configuration's directory, and use XML mode. Remote includes, XPointer, fallback, text includes, `xml:base`, DTDs/entity declarations, other encodings, and unsupported namespaces are rejected. If an installation uses these features, prepare a separately reviewed supported configuration layout before using this utility; the scan does not silently convert it.

Inactive configured datasets appear in the plan and can receive edits, but their IDs are excluded from reload requests. For shared include files, select every configuration that uses the fragment so the report can identify all affected datasets; files outside the selected include graphs are not discovered.

To supplement the local scan with currently served metadata, add:

```text
--server https://your-server.example/erddap
```

Use the URL of the installation being changed. The example OOI site is an overview and public service, not a substitute for the administrator's target server. HTTP inventory uses ERDDAP's [public REST metadata endpoints](https://erddap.dataexplorer.oceanobservatories.org/erddap/rest.html). It does not discover every private, inactive, or failed-to-load dataset. Request failures indicate incomplete coverage, not no matches.

Inspect `plan.json.report.txt` and the `review` output. Confirm the selected dataset, variable, attribute, old value, and proposed value for every edit. Check manual-action findings, input coverage, and affected reload IDs. Default matching ignores case and preserves all-uppercase or all-lowercase spelling; other matches use the supplied replacement. `--case-sensitive` narrows matching. Repeat `--attribute NAME` to add specific text attributes to the defaults. For a different scope or replacement, generate and review a new plan at a new path. Existing plans and reports are not overwritten.

Source-only/inherited metadata is reported for manual action. This utility edits existing `addAttributes`; it does not synthesize overrides from public metadata. Some ERDDAP dataset types, notably `EDDGridFromErddap` and `EDDTableFromErddap`, [prohibit those overrides](https://erddap.github.io/docs/server-admin/datasets#no-addattributes-axisvariable-or-datavariable).

## 4. Apply and request reloads

```bash
python3 erddap_admin_rename.py apply \
  --plan /secure/change/plan.json \
  --confirm FULL_PLAN_SHA256 \
  --backup-dir /secure/change/backups
```

Replace the digest placeholder with the full value from the reviewed plan. Apply validates the plan and current files before replacing them. If files changed since scanning, make a fresh plan rather than bypassing the mismatch. Keep the unique transaction directory and its manifest; these identify the backups used for rollback.

Inspect the applied configuration and manifest before requesting reloads:

```bash
python3 erddap_admin_rename.py review \
  --manifest /secure/change/backups/TRANSACTION/manifest.json
python3 erddap_admin_rename.py reload \
  --manifest /secure/change/backups/TRANSACTION/manifest.json \
  --big-parent /actual/erddapData \
  --confirm FULL_MANIFEST_SHA256
```

Use the actual manifest path and its reviewed digest. This separate command creates ordinary `flag/<datasetID>` files under the supplied `bigParentDirectory`. It does not restart Tomcat or use `hardFlag`. [Ordinary reloads](https://erddap.github.io/docs/server-admin/additional-information#flag) are sufficient for metadata edits and clear ERDDAP's cached dataset images and responses.

## 5. Verify and recover

Check `bigParentDirectory/logs/log.txt` for successful reloads of the affected datasets. Reloads are asynchronous; flag disappearance alone does not prove success. Inspect each affected dataset's metadata page, `/info/DATASET_ID/index.json`, and representative graphs. Repeat the scan into a new plan path to identify remaining matches and outstanding manual work.

Verify surfaces outside the utility's automatic scope separately: source NetCDF files or database strings, inherited metadata, prebuilt FGDC/ISO files, templates or other configuration, downstream websites, external basemaps, and old exported images. A successful configuration edit does not establish that all these surfaces changed. `long_name` can affect [ERDDAP graph axis labels](https://erddap.github.io/docs/server-admin/datasets#long_name), but text baked into other graphics has its own source. Dataset IDs, variable names, and URLs are not rename targets.

To restore the backed-up configuration:

```bash
python3 erddap_admin_rename.py review \
  --manifest /secure/change/backups/TRANSACTION/manifest.json
python3 erddap_admin_rename.py rollback \
  --manifest /secure/change/backups/TRANSACTION/manifest.json \
  --confirm FULL_MANIFEST_SHA256
```

Rollback checks the current files against the transaction before restoring them. A refusal because someone changed a file requires reconciliation of those later edits. After rollback, inspect the restored files. **Rollback changes the manifest and therefore its digest**: run `review --manifest` again, use the new digest with `reload`, and verify the restored metadata and graphs.

If a command reports a partial failure, retain its output and transaction directory, inspect the manifest and actual files, and resolve the reported failure before requesting reloads. Do not regard a failed command as a completed update.

XML parsing and local tests cannot prove that ERDDAP can load the real sources. ERDDAP's `DasDds` is available for installation-specific validation, but it [deletes cached dataset information](https://erddap.github.io/docs/server-admin/datasets#dasdds); use it deliberately on an appropriate test installation, not as a read-only scan step. The tool authors have not accessed or modified NOAA production servers.
