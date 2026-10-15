# NAS Google Photos album downloader

`scripts/download_albums.py` attaches to a Chromium instance that exposes CDP only inside its shared network namespace. It uses the currently signed-in Google Photos session; it does not automate login or store Google credentials in the repository.

## Runtime behavior

- waits for a manual sign-in;
- discovers album links and titles from the Google Photos UI;
- normalizes title matching for skip-list checks;
- opens separate pages for concurrent downloads;
- waits for Google Photos to prepare the ZIP;
- tracks Chromium `Browser.downloadProgress` events;
- stores downloads in a NAS-backed staging directory;
- verifies ZIP CRCs when `VERIFY_ZIP_CRC=true`;
- keeps a manifest and retries failed albums.

## Important environment variables

```text
CDP_URL=http://127.0.0.1:9222
ALBUMS_URL=https://photos.google.com/albums
ACCOUNT_LABEL=account-1
MAX_ALBUMS=0
MAX_CONCURRENT_DOWNLOADS=5
SKIP_ALBUMS=already-exported-album
VERIFY_ZIP_CRC=true
DOWNLOAD_EVENT_TIMEOUT_MS=900000
DOWNLOAD_COMPLETION_TIMEOUT_SECONDS=21600
```

Lower `MAX_CONCURRENT_DOWNLOADS` to `1` or `2` if Google Photos, Chromium, or the NAS becomes unstable. A `.zip.part` file is incomplete until the runner verifies and renames it.

## Security

Keep the Chromium `/config` mount private because it contains the persistent browser profile. Do not publish port 9222. Protect the web desktop behind a private network or authenticated reverse proxy. The downloader should only have access to the reference-download directory it needs.
