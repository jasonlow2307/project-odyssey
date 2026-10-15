# Bundle manifest

This document records what was gathered for the public GitHub bundle, where it came from, and what was deliberately left out.

## Included artifacts

### Google Photos album reconciliation

- `scripts/google_album_reconciler.py` — canonical reconciliation tool. It builds an inventory, reads reference ZIPs, creates a dry-run plan, matches by checksum, attaches existing assets to albums, optionally uploads missing assets through `immich-go`, records journals, and supports rollback of changes recorded in a journal.
- `config/google_album_reconciler.example.json` — sanitized multi-user configuration using placeholder users, paths, environment-variable names, and albums.
- `config/google_album_reconciler.manifest.example.json` — sanitized reference manifest example.
- `deploy/album-reconciler.compose.yml` — generic container definition for running the reconciler with a mounted migration directory.
- `deploy/immich-go-terminal.compose.yml` — generic terminal container definition for running `immich-go` against the NAS.
- `docs/MIGRATION_GUIDE.md` and `docs/MIGRATION_GUIDE.pdf` — public-safe operating notes, recovery procedures, and storage guidance.

The source file and its local deployment copy were compared before packaging; the bundle keeps one canonical copy under `scripts/`.

### NAS Google Photos downloader

- `scripts/download_albums.py` — Chromium CDP downloader for Google Photos album ZIPs. It handles the signed-in browser session, native download completion, manifest output, and optional ZIP integrity checks.
- `deploy/google-photos-playwright.Dockerfile` — generic image definition.
- `deploy/google-photos-playwright.compose-addon.yml` — sanitized Compose add-on with placeholder paths and an example skip list.
- `docs/DOWNLOADER.md` — setup and operating notes.

### Duplicate discovery and cleanup

- `scripts/immich_cross_user_duplicates.py` — read-only checksum and CLIP-vector duplicate report for smaller libraries.
- `scripts/immich_cross_user_duplicates_batched.py` — checkpointed batched workflow for larger libraries.
- `sql/immich_cross_user_duplicate_pairs.sql` — read-only PostgreSQL report query.
- `requirements/duplicates.txt` and `requirements/duplicates-batched.txt` — dependency lists for the two Python implementations.
- `tests/test_tool.py` and `tests/test_batched_tool.py` — unit tests with sanitized fixture names.
- `scripts/immich-trash-duplicates.sh` — preview-first helper for moving independently reviewed duplicate candidates to Immich Trash. It does not permanently delete files.
- `tools/build_public_guide.py` — portable ReportLab helper for regenerating the public PDF from the Markdown guide; its dependency is listed in `requirements/docs.txt`.

## Source workstreams consulted

The artifacts were traced across the local migration workspace and the related Codex workspaces for:

1. Google Photos to Immich album reconciliation.
2. NAS-hosted Google Photos browser downloads.
3. Cross-user Immich duplicate discovery and cleanup.

## Deliberate exclusions

The following were not copied because they are private, generated, large, or unsafe for a public repository:

- API keys, database passwords, private hostnames, private IP addresses, personal names, email addresses, and real album names.
- Generated plans, journals, inventory databases, logs, upload staging, downloaded ZIPs, media, and browser profiles.
- The `immich-go` executable and other compiled binaries.
- NAS-specific compose files and private runtime configuration.
- The Vue visualizer's `node_modules` and build output; its source remains a separate project.
- A family-video compression shell script referenced by another conversation but not available as a verified local file in the shared workspaces. It was intentionally not reconstructed from memory.

## Review before publishing

Before committing, review the sanitized examples for the target Immich version, replace placeholders locally without committing secrets, and run the validation commands in `README.md`. Treat the included scripts as operational tools that still require a backup, a dry run, and an independent review of destructive actions.
