# Immich migration toolkit

Reusable scripts and deployment examples collected from the Google Photos -> NAS -> Immich migration work.

This bundle is prepared for a public GitHub repository:

- personal names, email addresses, private IPs, API keys, hostnames, and real album names are removed;
- live plans, journals, ZIP archives, media, caches, compiled bytecode, and NAS-specific configs are excluded;
- examples use placeholders such as `NAS_HOST`, `/HOST/MIGRATION_ROOT`, and `User A`.

The scripts are operational tools, not a turnkey Immich distribution. Review the target Immich version, API permissions, Docker mounts, database credentials, and available storage before running anything that changes data.

## Contents

| Area | Files | Purpose |
|---|---|---|
| Album reconciliation | `scripts/google_album_reconciler.py` | Plan, match, attach, optionally upload, journal, and roll back shared-album membership. |
| Google Photos download | `scripts/download_albums.py` | Drive a signed-in NAS Chromium session through CDP and download album ZIPs with progress and CRC checks. |
| Duplicate audit | `scripts/immich_cross_user_duplicates.py` | Read-only checksum and CLIP-vector duplicate report for smaller libraries. |
| Duplicate audit, batched | `scripts/immich_cross_user_duplicates_batched.py` | Checkpointed export/index/report workflow for larger libraries. |
| Duplicate SQL | `sql/immich_cross_user_duplicate_pairs.sql` | Read-only PostgreSQL trial that writes bounded CSV reports. |
| Duplicate cleanup | `scripts/immich-trash-duplicates.sh` | Safety-gated batch move of reviewed candidates to Immich Trash. |
| Deployment | `deploy/` | Generic Compose fragments for the reconciler, immich-go terminal, rclone, and Chromium downloader. |
| Examples | `config/` | Placeholder reconciler config and album manifest. |
| Tests | `tests/` | Unit tests for both duplicate-report implementations. |
| Documentation build | `tools/build_public_guide.py` | Rebuild the public PDF from `docs/MIGRATION_GUIDE.md` using portable ReportLab fonts. |
| Guide | `docs/MIGRATION_GUIDE.md` and `.pdf` | Public-safe operational guide. |
| Scenario matrix | `docs/SCENARIOS.md` | Supported workflows, safety boundaries, and verification status for every tool. |

## Quick start

### 1. Reconciler

Copy `config/google_album_reconciler.example.json`, replace the example albums and paths, and export keys without putting them in Git:

```bash
export IMMICH_USER_A_API_KEY='keep-this-out-of-shell-history-when-possible'
export IMMICH_USER_B_API_KEY='keep-this-out-of-shell-history-when-possible'
```

Run a read-only plan first:

```bash
python3 scripts/google_album_reconciler.py plan \
  --config config/google_album_reconciler.example.json \
  --output /srv/migration/reconciler-state/plan.json \
  --refresh-inventory
```

Inspect ambiguous matches, fallback matches, missing references, and external-upload candidates before applying. Apply an existing plan with a new journal:

```bash
python3 scripts/google_album_reconciler.py apply \
  --config config/google_album_reconciler.example.json \
  --plan /srv/migration/reconciler-state/plan.json \
  --journal /srv/migration/reconciler-state/apply-example.json \
  --yes
```

Use `--upload-missing` only after reviewing the plan. Use the rollback command documented in the guide and keep journals until verification is complete.

### 2. NAS Google Photos downloader

Build `deploy/google-photos-playwright.Dockerfile` from the repository root. It installs `requirements/downloader.txt` and copies `scripts/download_albums.py` into the image. Merge `deploy/google-photos-playwright.compose-addon.yml` into the existing Chromium/Selkies Compose project. Keep CDP port 9222 private to the Chromium network namespace.

Sign in manually inside the NAS browser, then start the downloader. The runner saves ZIPs to the mounted reference directory, waits for native Chromium download completion, writes a manifest, and optionally verifies ZIP CRCs.

### 3. Duplicate audit

For the batched workflow:

```bash
python3 -m pip install -r requirements/duplicates-batched.txt

python3 scripts/immich_cross_user_duplicates_batched.py all \
  --host immich_postgres \
  --port 5432 \
  --database immich \
  --user postgres \
  --state-dir ./duplicate-state \
  --output-dir ./trial-report \
  --batch-size 2000 \
  --source-limit 200 \
  --skip-exact \
  --max-distance 0.01 \
  --limit-groups 20
```

Supply the PostgreSQL password through `PGPASSWORD` or another secret manager. Do not commit it or place it in shell history.

### 4. Duplicate cleanup

The cleanup helper is preview-only by default. Its candidate CSV must use:

```text
api_key_env,asset_id
IMMICH_USER_A_API_KEY,00000000-0000-0000-0000-000000000001
```

Preview:

```bash
IMMICH_URL='http://NAS_HOST:2283' \
  CANDIDATES=/srv/migration/immich-dedupe-candidates.csv \
  bash scripts/immich-trash-duplicates.sh
```

Only after an independent review, set `CONFIRM_TRASH=YES`. The script uses `force:false` and therefore moves assets to Immich Trash rather than permanently deleting them. Verify ownership, albums, sharing, and backups before emptying Trash.

## Validation

Run syntax checks before committing:

```bash
python3 -m py_compile scripts/*.py
python3 -m unittest discover -s tests -p 'test_*.py'
bash -n scripts/immich-trash-duplicates.sh
```

Install the optional documentation dependency, then rebuild the PDF after editing the Markdown guide:

```bash
python3 -m pip install -r requirements/docs.txt
python3 tools/build_public_guide.py
```

The duplicate scanners require PostgreSQL connectivity and their listed dependencies. The reconciler requires the Immich API, the configured `immich-go` binary for explicit external uploads, and a writable state directory.

See [`docs/SCENARIOS.md`](docs/SCENARIOS.md) for the tested local surface and live-environment acceptance checks.

## Safety rules

1. Keep API keys and database passwords outside tracked files.
2. Use the reconciler `plan` command before every apply.
3. Do not enable filename/size fallback matching without reviewing every fallback item.
4. Treat ambiguous matches as a hard stop; do not guess.
5. Keep one journal per apply attempt and never reuse a journal path for an unrelated plan.
6. Never delete Immich library files directly from the filesystem.
7. Move duplicate candidates to Trash first, verify, and only then consider permanent deletion.
8. Keep ZIPs and staging directories outside the repository and monitor free space.
9. Keep Chromium CDP, Docker, PostgreSQL, SSH, SMB, and Immich management endpoints private.

## Provenance and exclusions

The bundle consolidates artifacts from the migration/reconciliation workstream, the NAS Chromium downloader workstream, and the cross-user duplicate-audit workstream. The original workspaces remain available locally for historical reference.

Intentionally excluded: real per-user configs, generated plans and journals, album manifests containing real names, API keys, database dumps, media archives, downloaded ZIPs, staging directories, compiled Python caches, the `immich-go` binary, and the separate Vue visualizer's `node_modules`/build output.
