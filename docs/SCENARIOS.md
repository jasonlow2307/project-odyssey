# Scenario and verification matrix

This document separates what is verified locally from what requires a live Immich, PostgreSQL, Chromium, or NAS environment. A passing syntax check does not prove that a live API has the expected permissions or version-specific response shape.

## Verification status

The repository was checked on 2026-10-15:

- Python compilation passed for every Python script.
- JSON parsing passed for every example configuration.
- Shell syntax validation passed for the Trash helper.
- All 8 duplicate-tool unit tests passed.
- Reconciler and duplicate-tool `--help` commands completed successfully.
- The downloader source compiled successfully, but its local `--help` command requires Playwright; the production Dockerfile installs that dependency.
- No live NAS, Immich, PostgreSQL, Chromium/CDP, Google account, or `immich-go` acceptance test was run from this workstation.

## Tool matrix

| Tool | Scenario | Expected behavior | Local status | Live acceptance needed |
|---|---|---|---|---|
| `scripts/google_album_reconciler.py` | Plan with cached inventory | Reads configuration, resolves owner-visible albums, scans reference ZIPs, writes a JSON plan, and makes no album changes. | CLI and compilation verified. | Run against a disposable/test album and inspect the plan. |
| Reconciler | Plan with `--refresh-inventory` | Rebuilds or refreshes the SQLite inventory from Immich metadata pages before matching. | CLI verified. | Confirm pagination, API rate limits, and cache size on the NAS. |
| Reconciler | Plan with `--rebuild-inventory` | Archives the old SQLite cache and creates a fresh inventory. | CLI verified. | Confirm the state directory has enough free space and the rebuilt counts match Immich. |
| Reconciler | Exact checksum match | Classifies the reference file as an existing asset and records its asset ID. | Matching code covered indirectly by source review. | Compare a known reference file against a known Immich asset. |
| Reconciler | Unique filename + size fallback | Refuses the apply unless `--allow-fallback` is explicit. | Guard present and CLI verified. | Review every fallback item before enabling it. |
| Reconciler | Ambiguous match | Stops before mutation and lists the candidates; it never guesses. | Guard present and source review verified. | Confirm a deliberately duplicated test fixture stops the apply. |
| Reconciler | Existing cross-user asset | Uses the source owner’s editor key to attach the asset to the destination album. | Source logic and logs reviewed. | Confirm each source user is an album editor and test one asset per user. |
| Reconciler | External missing asset | Refuses unless `--upload-missing` is explicit; then stages and uploads it under the album owner. | Guard and journal logic reviewed. | Test one small image and one video, then verify ownership and membership. |
| Reconciler | Upload returns success but verification is delayed | Records an upload failure instead of attaching an unverified asset; the journal remains retryable. | Journal logic reviewed. | Test with a video or a deliberately slow ML/thumbnail queue. |
| Reconciler | Targeted post-upload refresh | Refreshes recent owner assets and uses checksum fallback to verify uploads without rescanning every user. | Configuration and code paths reviewed. | Compare the targeted result with a one-time full inventory on a test run. |
| Reconciler | Album member provisioning | Adds configured users or promotes them to the requested editor/viewer role idempotently. | Configuration and journal paths reviewed. | Verify the API key used for membership changes has album-sharing permission. |
| Reconciler | Apply with a new journal | Adds existing assets, optionally uploads missing media, and records changes/events/uploads/failures. | CLI and journal structure reviewed. | Apply a small reviewed plan and inspect the final status. |
| Reconciler | Interrupted or partial apply | Existing membership is skipped on retry; a new journal is used for the retry. | Documented and journal logic reviewed. | Interrupt a small test run and resume with a fresh journal. |
| Reconciler | Rollback | Removes only album memberships recorded by the selected journal and membership additions made by that run. It does not delete uploaded media. | CLI verified and rollback scope reviewed. | Roll back a disposable test journal and verify media remains. |
| `scripts/download_albums.py` | Signed-in Chromium session | Connects over CDP, discovers visible album pages, and uses the existing browser login. | Source compiled; Docker dependency path fixed. | Build the image and connect to the real Chromium service. |
| Downloader | Concurrent downloads | Opens separate pages up to `MAX_CONCURRENT_DOWNLOADS`; lower the value if Chromium/NAS is unstable. | Source review. | Run with 1, then 2–5 concurrent downloads. |
| Downloader | Skip list | Normalizes album titles and skips entries matching `SKIP_ALBUMS`. | Source review. | Test a temporary skip entry and confirm no download begins. |
| Downloader | Partial download | Keeps an incomplete `.zip.part` until native Chromium completion and ZIP verification succeed. | Source review. | Stop/restart a download and verify the partial file is recovered or cleaned safely. |
| Downloader | CRC verification | Verifies ZIP integrity when `VERIFY_ZIP_CRC=true`; invalid archives are not treated as complete. | Source review. | Test with a deliberately truncated archive. |
| Downloader | Retry and manifest | Retries navigation/album failures and records run status in the manifest. | Source review. | Force one failed album and confirm the next run retries it. |
| `scripts/immich_cross_user_duplicates.py` | Read-only exact duplicate report | Reads checksums and metadata in a read-only transaction and writes reports without changing Immich. | Unit tests and CLI verified. | Run with a read-only PostgreSQL account and confirm no table changes. |
| Single-pass duplicate tool | Visual duplicate report | Uses CLIP embeddings and a distance threshold to report likely cross-user duplicates. | Unit tests and CLI verified. | Confirm the Immich version has the expected embedding columns and test a small source limit. |
| `scripts/immich_cross_user_duplicates_batched.py` | Export checkpoint | Streams embeddings/metadata into local state and can resume after interruption. | Unit tests and CLI verified. | Interrupt export and confirm the checkpoint resumes without duplicate rows. |
| Batched duplicate tool | Local HNSW index | Builds the local index without holding a long vector query open in PostgreSQL. | Unit tests and CLI verified. | Run on a representative state directory and monitor RAM/disk. |
| Batched duplicate tool | Report restart | Searches the local index and writes exact/visual CSV and HTML reports. | Unit tests and CLI verified. | Compare a bounded report with the single-pass tool on a small sample. |
| `sql/immich_cross_user_duplicate_pairs.sql` | Read-only SQL trial | Produces a bounded duplicate-pair CSV using PostgreSQL data. | File inspection only. | Run inside a transaction/read-only role and verify the output before any cleanup. |
| `scripts/immich-trash-duplicates.sh` | Preview | Counts valid candidate rows and makes no DELETE request. | Shell syntax verified. | Run with a candidate CSV and inspect the count. |
| Trash helper | Reviewed move to Trash | Groups candidates by API-key environment variable and calls Immich with `force:false`. | Source review. | Test on disposable assets, verify Trash, and only then process production candidates. |
| Trash helper | Missing key or invalid ID | Skips missing-key rows or invalid UUIDs with warnings; API failures stop the run. | Source review. | Confirm the operational policy for skipped rows before production use. |
| `tools/build_public_guide.py` | Rebuild PDF | Converts the public Markdown guide to the checked-in PDF using ReportLab built-in fonts. | Source compiles. | Install `requirements/docs.txt` and visually inspect the PDF after edits. |

## Recommended live acceptance sequence

### 1. Reconciler smoke test

Use one small test album and a dedicated state directory:

```bash
python3 scripts/google_album_reconciler.py plan \
  --config config/google_album_reconciler.example.json \
  --output /srv/migration/reconciler-state/smoke-plan.json \
  --refresh-inventory
```

Check that the plan has:

- no missing owner-visible album;
- zero ambiguous items;
- zero fallback items unless individually approved;
- expected exact/already-in-album counts;
- external items explicitly reviewed before upload.

Apply only the reviewed test plan:

```bash
python3 scripts/google_album_reconciler.py apply \
  --config config/google_album_reconciler.example.json \
  --plan /srv/migration/reconciler-state/smoke-plan.json \
  --journal /srv/migration/reconciler-state/smoke-apply.json \
  --yes
```

Then verify the journal and, if needed, test rollback:

```bash
python3 scripts/google_album_reconciler.py rollback \
  --config config/google_album_reconciler.example.json \
  --journal /srv/migration/reconciler-state/smoke-apply.json \
  --yes
```

### 2. Downloader smoke test

Build from the repository root so the Dockerfile can copy `scripts/download_albums.py`:

```bash
docker build -f deploy/google-photos-playwright.Dockerfile -t google-photos-playwright .
```

Use one small album, `MAX_CONCURRENT_DOWNLOADS=1`, and CRC verification. Confirm the resulting ZIP opens and the manifest records `downloaded` rather than `failed` or `download_unavailable`.

### 3. Duplicate-report smoke test

Use a read-only database account and a small source limit first. For the batched tool:

```bash
python3 -m pip install -r requirements/duplicates-batched.txt
python3 scripts/immich_cross_user_duplicates_batched.py all \
  --host immich_postgres \
  --port 5432 \
  --database immich \
  --user readonly_reporter \
  --state-dir ./duplicate-state-smoke \
  --output-dir ./duplicate-report-smoke \
  --source-limit 100 \
  --skip-exact
```

Review the CSV/HTML output. Do not feed candidates into the Trash helper until ownership, album membership, and backup status are independently confirmed.

## Known boundaries

- The reconciler depends on Immich API behavior and permissions that cannot be proven offline.
- The downloader depends on Google Photos UI selectors, a valid signed-in browser session, and a private CDP connection.
- The duplicate tools depend on Immich PostgreSQL schema compatibility and installed CLIP/HNSW dependencies.
- The Trash helper is intentionally conservative but cannot determine whether a candidate is safe for your specific library; review remains mandatory.
- The repository does not include `immich-go`, browser profiles, API keys, database credentials, ZIPs, or media fixtures. Those must be supplied at deployment time.
