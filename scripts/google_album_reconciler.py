#!/usr/bin/env python3
"""Safely reconcile Google Photos reference albums into Immich albums.

The tool is deliberately split into three operations:

* ``plan`` builds a read-only matching plan and saves it as JSON.
* ``apply`` adds existing asset IDs and, when explicitly enabled, uploads
  missing reference media under the album owner before adding it.
* ``rollback`` removes only the album memberships recorded by ``apply``.

API keys are read from environment variables named by the JSON configuration
file. They are never written to plans or journals.

The script uses exact SHA-1 content matches first. A unique filename+size
match is an optional conservative fallback. Ambiguous files are reported and
are never applied automatically. Missing reference media is uploaded only
when ``apply --upload-missing`` is explicitly used.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TOOL_VERSION = 6
DEFAULT_PAGE_SIZE = 1000
DEFAULT_BATCH_SIZE = 100
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_RETRIES = 4
DEFAULT_PLAN_LOG_INTERVAL = 250

MEDIA_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".arw",
    ".avif",
    ".avi",
    ".bmp",
    ".cr2",
    ".cr3",
    ".dng",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".m2t",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mod",
    ".mov",
    ".mp4",
    ".mpg",
    ".mts",
    ".nef",
    ".orf",
    ".png",
    ".raf",
    ".raw",
    ".rw2",
    ".tif",
    ".tiff",
    ".tod",
    ".webm",
    ".webp",
    ".wmv",
}


class ReconcileError(RuntimeError):
    """A user-actionable reconciliation error."""


class AlbumNotFoundError(ReconcileError):
    """An album could not be found for its configured owner."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def utc_now_minus(seconds: int) -> str:
    return (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)
    ).replace(microsecond=0).isoformat()


def fail(message: str) -> None:
    raise ReconcileError(message)


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        json.dump(value, temporary, indent=2, ensure_ascii=False)
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as error:
        fail(f"File not found: {path}")
    except json.JSONDecodeError as error:
        fail(f"Invalid JSON in {path}: {error}")


def normalized_filename(name: str) -> str:
    """Normalize only what is safe to normalize for a fallback match."""

    basename = name.replace("\\", "/").rsplit("/", 1)[-1]
    normalized = unicodedata.normalize("NFKC", basename).casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def normalized_album_name(name: str) -> str:
    """Normalize Unicode form and outer whitespace for album-name lookup."""

    return unicodedata.normalize("NFKC", name).strip()


def sha1_stream(stream: Any, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha1()
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def sha1_file(path: Path) -> str:
    with path.open("rb") as handle:
        return sha1_stream(handle)


def sha1_zip_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    with archive.open(info, "r") as stream:
        return sha1_stream(stream)


def is_media_name(name: str) -> bool:
    return Path(name).suffix.casefold() in MEDIA_EXTENSIONS


def media_sources(reference: str | Path) -> list[tuple[str, Path | None, zipfile.ZipInfo | None]]:
    """Return entries as (display name, local path, zip info)."""

    path = Path(reference)
    if not path.exists():
        fail(f"Reference path does not exist: {path}")

    if path.is_dir():
        return [
            (str(file_path), file_path, None)
            for file_path in sorted(path.rglob("*"))
            if file_path.is_file() and is_media_name(file_path.name)
        ]

    if path.suffix.casefold() != ".zip":
        fail(f"Reference must be a ZIP file or directory of media files: {path}")

    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:
        fail(f"Invalid ZIP archive {path}: {error}")

    entries: list[tuple[str, Path | None, zipfile.ZipInfo | None]] = []
    for info in archive.infolist():
        if info.is_dir() or not is_media_name(info.filename):
            continue
        entries.append((f"{path.name}:{info.filename}", None, info))
    archive.close()
    return entries


@dataclass(frozen=True)
class ReferenceMedia:
    display_name: str
    checksum: str
    file_size: int
    source_path: str
    archive_member: str | None


def iter_reference_files(
    references: Iterable[str | Path],
) -> Iterable[ReferenceMedia]:
    """Yield reference media and enough source information to upload it later."""

    for reference in references:
        path = Path(reference)
        if path.is_dir():
            for local_path in sorted(path.rglob("*")):
                if not local_path.is_file() or not is_media_name(local_path.name):
                    continue
                yield ReferenceMedia(
                    display_name=str(local_path),
                    checksum=sha1_file(local_path),
                    file_size=local_path.stat().st_size,
                    source_path=str(local_path),
                    archive_member=None,
                )
            continue

        try:
            archive = zipfile.ZipFile(path)
        except zipfile.BadZipFile as error:
            fail(f"Invalid ZIP archive {path}: {error}")
        try:
            for info in archive.infolist():
                if info.is_dir() or not is_media_name(info.filename):
                    continue
                yield ReferenceMedia(
                    display_name=f"{path.name}:{info.filename}",
                    checksum=sha1_zip_entry(archive, info),
                    file_size=info.file_size,
                    source_path=str(path),
                    archive_member=info.filename,
                )
        finally:
            archive.close()


def decode_immich_checksum(value: Any) -> str | None:
    """Convert Immich's base64 or hexadecimal SHA-1 checksum to hex."""

    if not isinstance(value, str) or not value:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{40}", value):
        return value.casefold()
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    if len(raw) != hashlib.sha1().digest_size:
        return None
    return raw.hex()


def encode_immich_checksum(value: str) -> str:
    """Encode a hexadecimal SHA-1 checksum for Immich's search API."""

    if not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        fail(f"Invalid SHA-1 checksum: {value!r}")
    return base64.b64encode(bytes.fromhex(value)).decode("ascii")


def extract_asset_size(asset: dict[str, Any]) -> int | None:
    for key in ("size", "fileSize", "fileSizeInByte"):
        value = asset.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)

    exif_info = asset.get("exifInfo")
    if isinstance(exif_info, dict):
        value = exif_info.get("fileSizeInByte")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


@dataclass(frozen=True)
class User:
    name: str
    api_key_env: str
    api_key: str
    user_id: str | None = None


class ImmichClient:
    def __init__(
        self,
        server: str,
        api_key: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        retries: int = DEFAULT_RETRIES,
    ) -> None:
        self.server = server.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.retries = retries

    def request(
        self,
        method: str,
        endpoint: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.server}/api/{endpoint.lstrip('/')}"
        payload = None
        headers = {
            "Accept": "application/json",
            "x-api-key": self.api_key,
        }
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            request = Request(url, data=payload, headers=headers, method=method)
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    response_body = response.read()
                    if not response_body:
                        return None
                    return json.loads(response_body.decode("utf-8"))
            except HTTPError as error:
                response_body = error.read().decode("utf-8", errors="replace")
                last_error = ReconcileError(
                    f"Immich API {method} {endpoint} returned HTTP {error.code}: "
                    f"{response_body[:1000]}"
                )
                if error.code not in {408, 429, 500, 502, 503, 504}:
                    break
                retry_after = error.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                if attempt < self.retries:
                    print(
                        f"[API] retrying {method} {endpoint} after HTTP {error.code}; "
                        f"attempt {attempt + 2}/{self.retries + 1} in {min(delay, 60):.0f}s",
                        flush=True,
                    )
                    time.sleep(min(delay, 60))
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
                last_error = error
                delay = min(2**attempt, 60)
                if attempt < self.retries:
                    print(
                        f"[API] retrying {method} {endpoint} after {type(error).__name__}; "
                        f"attempt {attempt + 2}/{self.retries + 1} in {delay:.0f}s",
                        flush=True,
                    )
                    time.sleep(delay)

        assert last_error is not None
        if isinstance(last_error, ReconcileError):
            raise last_error
        raise ReconcileError(f"Immich API {method} {endpoint} failed: {last_error}")

    def get_albums(self) -> list[dict[str, Any]]:
        result = self.request("GET", "albums")
        if not isinstance(result, list):
            fail("Immich returned an unexpected response from GET /api/albums")
        return result

    def get_current_user_id(self) -> str:
        result = self.request("GET", "users/me")
        if not isinstance(result, dict) or not isinstance(result.get("id"), str):
            fail("Immich returned an unexpected response from GET /api/users/me")
        return result["id"]

    def get_album(self, album_id: str) -> dict[str, Any]:
        result = self.request("GET", f"albums/{album_id}")
        if not isinstance(result, dict):
            fail(f"Immich returned an unexpected response for album {album_id}")
        return result

    def search_assets(
        self,
        page: int,
        page_size: int,
        album_ids: list[str] | None = None,
        checksum: str | None = None,
        created_after: str | None = None,
    ) -> dict[str, Any]:
        """Use Immich's stable metadata-search endpoint with pagination."""

        body = {
            "page": page,
            "size": page_size,
            "withArchived": True,
            "withDeleted": False,
            "withExif": True,
            "withPeople": False,
            "withStacked": True,
            "order": "asc",
        }
        if album_ids:
            body["albumIds"] = album_ids
        if checksum:
            body["checksum"] = checksum
        if created_after:
            body["createdAfter"] = created_after
        result = self.request("POST", "search/metadata", body)
        if not isinstance(result, dict) or not isinstance(result.get("assets"), dict):
            fail("Immich returned an unexpected response from POST /api/search/metadata")
        return result

    def get_album_asset_ids(self, album_id: str, page_size: int) -> set[str]:
        """Return album membership using metadata search's album filter.

        Current Immich versions intentionally omit the full asset list from
        GET /api/albums/{id}; they return album metadata and assetCount only.
        Searching with albumIds is the supported way to enumerate membership.
        """

        asset_ids: set[str] = set()
        page = 1
        while True:
            result = self.search_assets(page, page_size, album_ids=[album_id])
            asset_page = result["assets"]
            items = asset_page.get("items") or []
            if not isinstance(items, list):
                fail(f"Immich returned invalid album asset items on page {page}")
            for asset in items:
                if isinstance(asset, dict) and isinstance(asset.get("id"), str):
                    asset_ids.add(asset["id"])
            next_page = asset_page.get("nextPage")
            if not next_page:
                break
            page = int(next_page)
        return asset_ids

    def get_all_assets(
        self,
        page_size: int,
        user_name: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._get_filtered_assets(page_size, user_name=user_name)

    def get_assets_created_after(
        self,
        page_size: int,
        created_after: str,
        user_name: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._get_filtered_assets(
            page_size,
            user_name=user_name,
            created_after=created_after,
        )

    def get_assets_by_checksums(
        self,
        page_size: int,
        checksums: Iterable[str],
        user_name: str | None = None,
    ) -> list[dict[str, Any]]:
        assets: list[dict[str, Any]] = []
        unique_checksums = sorted(set(checksums))
        for index, checksum in enumerate(unique_checksums, start=1):
            encoded_checksum = encode_immich_checksum(checksum)
            print(
                f"[INVENTORY] targeted checksum lookup {index}/{len(unique_checksums)}"
                f" user={user_name or 'current'}",
                flush=True,
            )
            assets.extend(
                self._get_filtered_assets(
                    page_size,
                    user_name=user_name,
                    checksum=encoded_checksum,
                )
            )
        return assets

    def _get_filtered_assets(
        self,
        page_size: int,
        user_name: str | None = None,
        checksum: str | None = None,
        created_after: str | None = None,
    ) -> list[dict[str, Any]]:
        assets: list[dict[str, Any]] = []
        page = 1
        label = f" user={user_name}" if user_name else ""
        filters: list[str] = []
        if checksum:
            filters.append("checksum")
        if created_after:
            filters.append(f"createdAfter={created_after}")
        filter_label = f" filters={','.join(filters)}" if filters else ""
        while True:
            started = time.monotonic()
            print(
                f"[INVENTORY] requesting asset page={page}{label}{filter_label}",
                flush=True,
            )
            result = self.search_assets(
                page,
                page_size,
                checksum=checksum,
                created_after=created_after,
            )
            asset_page = result["assets"]
            items = asset_page.get("items") or []
            if not isinstance(items, list):
                fail(f"Immich returned invalid asset items on page {page}")
            assets.extend(item for item in items if isinstance(item, dict))
            next_page = asset_page.get("nextPage")
            elapsed = time.monotonic() - started
            print(
                f"[INVENTORY] received asset page={page}{label}{filter_label} "
                f"assets={len(items)} elapsed={elapsed:.1f}s next_page={next_page!r}",
                flush=True,
            )
            if not next_page:
                break
            page = int(next_page)
        return assets

    def add_assets(self, album_id: str, asset_ids: list[str]) -> Any:
        return self.request("PUT", f"albums/{album_id}/assets", {"ids": asset_ids})

    def add_user(self, album_id: str, user_id: str, role: str) -> Any:
        return self.request(
            "PUT",
            f"albums/{album_id}/users",
            {"albumUsers": [{"userId": user_id, "role": role}]},
        )

    def remove_user(self, album_id: str, user_id: str) -> Any:
        return self.request("DELETE", f"albums/{album_id}/user/{user_id}")

    def remove_assets(self, album_id: str, asset_ids: list[str]) -> Any:
        return self.request("DELETE", f"albums/{album_id}/assets", {"ids": asset_ids})


class Inventory:
    """SQLite-backed asset inventory for efficient repeated planning."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS assets (
                user_name TEXT NOT NULL,
                asset_id TEXT NOT NULL,
                checksum TEXT,
                filename TEXT NOT NULL,
                normalized_filename TEXT NOT NULL,
                file_size INTEGER,
                file_created_at TEXT,
                asset_type TEXT,
                PRIMARY KEY (user_name, asset_id)
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_assets_checksum ON assets(checksum)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_assets_filename_size "
            "ON assets(normalized_filename, file_size)"
        )
        self.connection.commit()

    def replace_user(self, user_name: str, assets: Iterable[dict[str, Any]]) -> None:
        self.connection.execute("DELETE FROM assets WHERE user_name = ?", (user_name,))
        self._write_assets(user_name, assets)
        self.connection.commit()

    def upsert_user(self, user_name: str, assets: Iterable[dict[str, Any]]) -> None:
        """Merge a targeted asset response without deleting the cached inventory."""

        self._write_assets(user_name, assets)
        self.connection.commit()

    def _write_assets(self, user_name: str, assets: Iterable[dict[str, Any]]) -> None:
        rows = []
        for asset in assets:
            asset_id = asset.get("id")
            filename = asset.get("originalFileName")
            if not isinstance(asset_id, str) or not isinstance(filename, str):
                continue
            rows.append(
                (
                    user_name,
                    asset_id,
                    decode_immich_checksum(asset.get("checksum")),
                    filename,
                    normalized_filename(filename),
                    extract_asset_size(asset),
                    asset.get("fileCreatedAt"),
                    asset.get("type"),
                )
            )
        self.connection.executemany(
            """
            INSERT INTO assets(
                user_name, asset_id, checksum, filename,
                normalized_filename, file_size, file_created_at, asset_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_name, asset_id) DO UPDATE SET
                checksum = excluded.checksum,
                filename = excluded.filename,
                normalized_filename = excluded.normalized_filename,
                file_size = excluded.file_size,
                file_created_at = excluded.file_created_at,
                asset_type = excluded.asset_type
            """,
            rows,
        )

    def by_checksum(self, checksum: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT user_name, asset_id, filename, file_size, file_created_at, asset_type "
            "FROM assets WHERE checksum = ? ORDER BY user_name, asset_id",
            (checksum,),
        ).fetchall()
        return [self._row(row) for row in rows]

    def by_filename_size(self, filename: str, file_size: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT user_name, asset_id, filename, file_size, file_created_at, asset_type "
            "FROM assets WHERE normalized_filename = ? AND file_size = ? "
            "ORDER BY user_name, asset_id",
            (normalized_filename(filename), file_size),
        ).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "user": row[0],
            "asset_id": row[1],
            "filename": row[2],
            "file_size": row[3],
            "file_created_at": row[4],
            "asset_type": row[5],
        }


def expand_reference_manifest(
    config: dict[str, Any],
    manifest_path: Path,
    user_names: set[str],
) -> list[dict[str, Any]]:
    """Expand a reference directory manifest into normal album entries.

    The manifest is deliberately required to map each ZIP to an exact album
    name and owner. The script refuses to ignore ZIPs or silently infer an
    owner, which prevents accidental cross-user album assignments.
    """

    reference_directory_value = config.get("reference_directory")
    if not isinstance(reference_directory_value, str) or not reference_directory_value:
        fail(
            "A reference_directory is required when reference_manifest is configured"
        )
    reference_directory = Path(reference_directory_value).resolve()
    if not reference_directory.is_dir():
        fail(f"Reference directory does not exist or is not a directory: {reference_directory}")

    manifest = read_json(manifest_path)
    if isinstance(manifest, dict):
        raw_entries = manifest.get("albums")
    else:
        raw_entries = manifest
    if not isinstance(raw_entries, list) or not raw_entries:
        fail("Reference manifest must contain a non-empty 'albums' list")

    discovered: dict[str, Path] = {}
    for file_path in sorted(reference_directory.rglob("*")):
        if file_path.is_file() and file_path.suffix.casefold() == ".zip":
            relative_name = file_path.relative_to(reference_directory).as_posix()
            discovered[relative_name] = file_path

    if not discovered:
        fail(f"No ZIP files found in reference directory: {reference_directory}")

    albums: list[dict[str, Any]] = []
    listed: set[str] = set()
    for index, raw_entry in enumerate(raw_entries, start=1):
        if not isinstance(raw_entry, dict):
            fail(f"Reference manifest entry {index} must be an object")
        zip_name = raw_entry.get("zip")
        album_name = raw_entry.get("name")
        owner = raw_entry.get("owner")
        if not isinstance(zip_name, str) or not zip_name:
            fail(f"Reference manifest entry {index} needs a non-empty 'zip'")
        if Path(zip_name).is_absolute():
            fail(f"Reference manifest entry {index} must use a path relative to reference_directory")
        if not isinstance(album_name, str) or not album_name:
            fail(f"Reference manifest entry {index} needs a non-empty 'name'")
        if not isinstance(owner, str) or owner not in user_names:
            fail(
                f"Reference manifest entry {index} has unknown owner {owner!r}; "
                f"expected one of: {', '.join(sorted(user_names))}"
            )

        normalized_name = Path(zip_name).as_posix()
        if normalized_name not in discovered:
            fail(
                f"Reference manifest entry {index} points to missing or non-ZIP file: "
                f"{zip_name}"
            )
        if normalized_name in listed:
            fail(f"Reference ZIP is listed more than once: {zip_name}")
        listed.add(normalized_name)

        album_entry: dict[str, Any] = {
            "name": album_name,
            "owner": owner,
            "reference": [str(discovered[normalized_name])],
        }
        configured_id = raw_entry.get("id")
        if configured_id is not None:
            if not isinstance(configured_id, str) or not configured_id:
                fail(f"Reference manifest entry {index} has an invalid 'id'")
            album_entry["id"] = configured_id
        albums.append(album_entry)

    unlisted = sorted(set(discovered) - listed)
    if unlisted:
        preview = ", ".join(unlisted[:10])
        suffix = " ..." if len(unlisted) > 10 else ""
        fail(
            f"Reference directory contains ZIPs missing from the manifest: "
            f"{preview}{suffix}"
        )
    return albums


def load_config(path: Path) -> tuple[dict[str, Any], list[User]]:
    config = read_json(path)
    if not isinstance(config, dict):
        fail("Configuration root must be a JSON object")

    server = config.get("server")
    if not isinstance(server, str) or not server.startswith(("http://", "https://")):
        fail("Configuration must contain an http(s) 'server'")

    raw_users = config.get("users")
    if not isinstance(raw_users, list) or not raw_users:
        fail("Configuration must contain a non-empty 'users' list")

    users: list[User] = []
    seen_names: set[str] = set()
    for raw_user in raw_users:
        if not isinstance(raw_user, dict):
            fail("Each user must be an object")
        name = raw_user.get("name")
        api_key_env = raw_user.get("api_key_env")
        if not isinstance(name, str) or not name.strip():
            fail("Each user needs a non-empty 'name'")
        if name in seen_names:
            fail(f"Duplicate user name: {name}")
        if not isinstance(api_key_env, str) or not api_key_env:
            fail(f"User {name} needs 'api_key_env'")
        api_key = os.environ.get(api_key_env)
        if not api_key:
            fail(f"Environment variable {api_key_env} is not set for user {name}")
        configured_id = raw_user.get("id")
        if configured_id is not None and (
            not isinstance(configured_id, str) or not configured_id
        ):
            fail(f"User {name} has an invalid 'id'")
        users.append(User(name, api_key_env, api_key, configured_id))
        seen_names.add(name)

    manifest_value = config.get("reference_manifest")
    albums = config.get("albums")
    if manifest_value is not None:
        if not isinstance(manifest_value, str) or not manifest_value:
            fail("reference_manifest must be a non-empty path")
        if albums not in (None, []):
            fail("Use either 'albums' or 'reference_manifest', not both")
        config["albums"] = expand_reference_manifest(
            config,
            Path(manifest_value),
            seen_names,
        )
    elif not isinstance(albums, list) or not albums:
        fail("Configuration must contain a non-empty 'albums' list")

    validate_album_member_config(config, seen_names)
    return config, users


def users_by_name(users: list[User]) -> dict[str, User]:
    return {user.name: user for user in users}


def album_member_config(
    config: dict[str, Any],
    user_names: Iterable[str],
) -> tuple[list[str], str]:
    """Return configured album members and their shared role."""

    raw_members = config.get("album_members")
    if raw_members is None:
        return [], "editor"
    if not isinstance(raw_members, dict):
        fail("'album_members' must be an object")

    raw_names = raw_members.get("users")
    if raw_names == "all":
        member_names = list(user_names)
    elif isinstance(raw_names, list) and all(
        isinstance(name, str) and name for name in raw_names
    ):
        member_names = list(dict.fromkeys(raw_names))
    else:
        fail("'album_members.users' must be 'all' or a list of user names")

    unknown = sorted(set(member_names) - set(user_names))
    if unknown:
        fail(
            "'album_members.users' contains unknown users: "
            + ", ".join(unknown)
        )

    role = raw_members.get("role", "editor")
    if role not in {"viewer", "editor"}:
        fail("'album_members.role' must be 'viewer' or 'editor'")
    return member_names, role


def validate_album_member_config(
    config: dict[str, Any],
    user_names: Iterable[str],
) -> None:
    album_member_config(config, user_names)


def resolve_user_ids(
    users: list[User],
    server: str,
    timeout_seconds: int,
    retries: int,
) -> dict[str, str]:
    """Resolve configured users to Immich IDs without requiring an admin key."""

    resolved: dict[str, str] = {}
    for user in users:
        if user.user_id is not None:
            resolved[user.name] = user.user_id
            continue
        client = ImmichClient(server, user.api_key, timeout_seconds, retries)
        resolved[user.name] = client.get_current_user_id()

    ids = list(resolved.values())
    if len(ids) != len(set(ids)):
        fail("Configured users resolved to duplicate Immich user IDs")
    return resolved


def album_user_ids(album: dict[str, Any]) -> set[str]:
    """Extract member IDs from an Immich album response."""

    result: set[str] = set()
    raw_users = album.get("albumUsers")
    if not isinstance(raw_users, list):
        return result
    for entry in raw_users:
        if not isinstance(entry, dict):
            continue
        direct_id = entry.get("userId")
        if isinstance(direct_id, str):
            result.add(direct_id)
            continue
        user = entry.get("user")
        if isinstance(user, dict) and isinstance(user.get("id"), str):
            result.add(user["id"])
    return result


def album_user_roles(album: dict[str, Any]) -> dict[str, str]:
    """Extract each album member's ID and sharing role."""

    result: dict[str, str] = {}
    raw_users = album.get("albumUsers")
    if not isinstance(raw_users, list):
        return result
    for entry in raw_users:
        if not isinstance(entry, dict):
            continue
        user_id = entry.get("userId")
        if not isinstance(user_id, str):
            user = entry.get("user")
            user_id = user.get("id") if isinstance(user, dict) else None
        role = entry.get("role")
        if isinstance(user_id, str) and isinstance(role, str):
            result[user_id] = role
    return result


def resolve_album(
    album_config: dict[str, Any],
    user_map: dict[str, User],
    server: str,
    timeout_seconds: int,
    retries: int,
    page_size: int,
    fetch_existing_ids: bool = True,
) -> tuple[str, str, str, set[str]]:
    name = album_config.get("name")
    owner = album_config.get("owner")
    configured_id = album_config.get("id")
    if not isinstance(name, str) or not name:
        fail("Each album needs a non-empty 'name'")
    if not isinstance(owner, str) or owner not in user_map:
        fail(f"Album {name!r} must name an owner from the users list")

    client = ImmichClient(server, user_map[owner].api_key, timeout_seconds, retries)
    if configured_id is not None:
        if not isinstance(configured_id, str) or not configured_id:
            fail(f"Album {name!r} has an invalid id")
        album = client.get_album(configured_id)
        actual_name = album.get("albumName")
        if not isinstance(actual_name, str):
            fail(f"Configured album id {configured_id} has no valid album name")
        if actual_name != name and normalized_album_name(actual_name) != normalized_album_name(name):
            fail(
                f"Configured album id {configured_id} is named "
                f"{actual_name!r}, not {name!r}"
            )
        if actual_name != name:
            print(
                f"[PLAN] normalized album-name match configured={name!r} "
                f"server={actual_name!r}",
                flush=True,
            )
    else:
        albums = client.get_albums()
        matches = [album for album in albums if album.get("albumName") == name]
        if not matches:
            normalized_name = normalized_album_name(name)
            normalized_matches = [
                album
                for album in albums
                if isinstance(album.get("albumName"), str)
                and normalized_album_name(album["albumName"]) == normalized_name
            ]
            if len(normalized_matches) == 1:
                matches = normalized_matches
                print(
                    f"[PLAN] normalized album-name match configured={name!r} "
                    f"server={matches[0]['albumName']!r}",
                    flush=True,
                )
            elif len(normalized_matches) > 1:
                names = ", ".join(
                    repr(str(album.get("albumName")))
                    for album in normalized_matches
                )
                fail(
                    f"Found multiple albums with normalized name {name!r} "
                    f"for {owner}: {names}. Set the intended album id explicitly."
                )
        if len(matches) == 0:
            raise AlbumNotFoundError(
                f"Could not find album {name!r} owned/visible to {owner}. "
                "Run the owner import with --sync-albums=true first, or set its id in config."
            )
        if len(matches) > 1:
            ids = ", ".join(str(album.get("id")) for album in matches)
            fail(
                f"Found multiple albums named {name!r} for {owner}: {ids}. "
                "Set the intended album id explicitly in config."
            )
        album = matches[0]

    existing_ids = (
        client.get_album_asset_ids(str(album["id"]), page_size)
        if fetch_existing_ids
        else set()
    )
    resolved_name = album.get("albumName")
    if not isinstance(resolved_name, str):
        resolved_name = name
    return str(album["id"]), resolved_name, owner, existing_ids


def refresh_inventory(
    config: dict[str, Any],
    users: list[User],
    inventory: Inventory,
    server: str,
) -> None:
    page_size = int(config.get("page_size", DEFAULT_PAGE_SIZE))
    timeout_seconds = int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    retries = int(config.get("retries", DEFAULT_RETRIES))
    for user in users:
        started = time.monotonic()
        print(f"[INVENTORY] starting user={user.name}", flush=True)
        client = ImmichClient(server, user.api_key, timeout_seconds, retries)
        assets = client.get_all_assets(page_size, user.name)
        print(
            f"[INVENTORY] fetched user={user.name} assets={len(assets)}; "
            "updating SQLite cache",
            flush=True,
        )
        inventory.replace_user(user.name, assets)
        print(
            f"[INVENTORY] completed user={user.name} cached={len(assets)} "
            f"elapsed={time.monotonic() - started:.1f}s",
            flush=True,
        )


def archive_inventory_cache(inventory_path: Path) -> None:
    """Move the SQLite cache aside so a completely fresh cache can be built."""

    cache_files = [
        inventory_path,
        Path(f"{inventory_path}-wal"),
        Path(f"{inventory_path}-shm"),
    ]
    existing_files = [path for path in cache_files if path.exists()]
    if not existing_files:
        print(f"[INVENTORY] no existing cache to archive at {inventory_path}", flush=True)
        return

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_base = inventory_path.with_name(
        f"{inventory_path.name}.backup-{timestamp}"
    )
    counter = 1
    while backup_base.exists():
        backup_base = inventory_path.with_name(
            f"{inventory_path.name}.backup-{timestamp}-{counter}"
        )
        counter += 1

    print(
        f"[INVENTORY] archiving existing cache to {backup_base}",
        flush=True,
    )
    for source in existing_files:
        suffix = source.name[len(inventory_path.name) :]
        destination = backup_base.with_name(backup_base.name + suffix)
        os.replace(source, destination)


def config_references(album_config: dict[str, Any]) -> list[str]:
    references = album_config.get("reference")
    if isinstance(references, str):
        return [references]
    if isinstance(references, list) and all(isinstance(item, str) for item in references):
        return references
    fail("Each album needs 'reference' as a path or list of paths")


def build_plan(config: dict[str, Any], users: list[User], inventory: Inventory) -> dict[str, Any]:
    server = str(config["server"]).rstrip("/")
    timeout_seconds = int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    retries = int(config.get("retries", DEFAULT_RETRIES))
    page_size = int(config.get("page_size", DEFAULT_PAGE_SIZE))
    user_map = users_by_name(users)
    match_config = config.get("match") if isinstance(config.get("match"), dict) else {}
    allow_filename_size = bool(match_config.get("allow_filename_size", True))
    plan_log_interval = int(
        config.get("plan_log_interval", DEFAULT_PLAN_LOG_INTERVAL)
    )
    if plan_log_interval < 1:
        fail("plan_log_interval must be at least 1")

    plan: dict[str, Any] = {
        "tool": "google_album_reconciler",
        "tool_version": TOOL_VERSION,
        "created_at": utc_now(),
        "server": server,
        "albums": [],
    }

    # Preflight every configured album and reference path before fetching
    # album membership or scanning any media. This makes missing albums visible
    # together and avoids spending time planning a partial configuration.
    preflight_missing_albums: list[dict[str, str]] = []
    preflight_missing_references: list[dict[str, str]] = []
    for album_config in config["albums"]:
        if not isinstance(album_config, dict):
            fail("Each album must be an object")
        name = str(album_config.get("name", "<unnamed>"))
        owner = str(album_config.get("owner", "<unknown>"))
        print(
            f"[PLAN] preflight album name={name!r} owner={owner!r}",
            flush=True,
        )
        try:
            resolve_album(
                album_config,
                user_map,
                server,
                timeout_seconds,
                retries,
                page_size,
                fetch_existing_ids=False,
            )
        except AlbumNotFoundError:
            preflight_missing_albums.append({"name": name, "owner": owner})
            print(
                f"[PLAN] preflight missing album name={name!r} owner={owner!r}",
                flush=True,
            )

        for reference in config_references(album_config):
            if not Path(reference).exists():
                preflight_missing_references.append(
                    {"album": name, "owner": owner, "reference": reference}
                )
                print(
                    f"[PLAN] preflight missing reference album={name!r} "
                    f"path={reference}",
                    flush=True,
                )

    if preflight_missing_albums or preflight_missing_references:
        validation_errors: list[str] = []
        if preflight_missing_albums:
            details = "\n".join(
                f"  - {missing['name']!r} (owner: {missing['owner']})"
                for missing in preflight_missing_albums
            )
            validation_errors.append(
                "Albums not found "
                f"({len(preflight_missing_albums)}):\n{details}"
            )
        if preflight_missing_references:
            details = "\n".join(
                f"  - {missing['reference']} "
                f"(album: {missing['album']!r}, owner: {missing['owner']})"
                for missing in preflight_missing_references
            )
            validation_errors.append(
                "Reference paths not found "
                f"({len(preflight_missing_references)}):\n{details}"
            )
        plan["partial"] = True
        plan["validation_errors"] = {
            "albums_not_found": preflight_missing_albums,
            "reference_paths_not_found": preflight_missing_references,
            "messages": validation_errors,
        }
        return plan

    missing_albums: list[dict[str, str]] = []
    missing_references: list[dict[str, str]] = []
    for album_config in config["albums"]:
        if not isinstance(album_config, dict):
            fail("Each album must be an object")
        print(
            f"[PLAN] resolving album name={album_config.get('name')!r} "
            f"owner={album_config.get('owner')!r}",
            flush=True,
        )
        try:
            album_id, album_name, owner, existing_ids = resolve_album(
                album_config, user_map, server, timeout_seconds, retries, page_size
            )
        except AlbumNotFoundError as error:
            name = str(album_config.get("name", "<unnamed>"))
            owner = str(album_config.get("owner", "<unknown>"))
            missing_albums.append({"name": name, "owner": owner})
            print(f"[PLAN] missing album name={name!r} owner={owner!r}", flush=True)
            continue
        references = config_references(album_config)
        missing_for_album = [
            reference for reference in references if not Path(reference).exists()
        ]
        if missing_for_album:
            album_name_for_error = str(album_config.get("name", album_name))
            album_owner_for_error = str(album_config.get("owner", owner))
            for reference in missing_for_album:
                missing_references.append(
                    {
                        "album": album_name_for_error,
                        "owner": album_owner_for_error,
                        "reference": reference,
                    }
                )
                print(
                    f"[PLAN] missing reference album={album_name_for_error!r} "
                    f"path={reference}",
                    flush=True,
                )
            continue

        print(
            f"[PLAN] resolved album={album_name!r} id={album_id} "
            f"existing_assets={len(existing_ids)}",
            flush=True,
        )
        for reference in references:
            print(f"[PLAN] scanning reference={reference}", flush=True)
        items: list[dict[str, Any]] = []
        counts = {
            "reference_media": 0,
            "matched_exact": 0,
            "matched_filename_size": 0,
            "already_in_album": 0,
            "external_missing": 0,
            "ambiguous": 0,
        }

        seen_reference_signatures: set[str] = set()
        started = time.monotonic()
        for media in iter_reference_files(references):
            display_name = media.display_name
            checksum = media.checksum
            file_size = media.file_size
            counts["reference_media"] += 1
            if counts["reference_media"] % plan_log_interval == 0:
                print(
                    f"[PLAN] album={album_name!r} processed={counts['reference_media']} "
                    f"exact={counts['matched_exact']} "
                    f"fallback={counts['matched_filename_size']} "
                    f"already_in_album={counts['already_in_album']} "
                    f"external={counts['external_missing']} "
                    f"ambiguous={counts['ambiguous']} "
                    f"elapsed={time.monotonic() - started:.1f}s",
                    flush=True,
                )
            if checksum in seen_reference_signatures:
                continue
            seen_reference_signatures.add(checksum)

            candidates = inventory.by_checksum(checksum)
            match_method = "sha1"
            if not candidates and allow_filename_size:
                filename = Path(media.archive_member or media.source_path).name
                candidates = inventory.by_filename_size(filename, file_size)
                match_method = "filename+size"

            base_item: dict[str, Any] = {
                "reference": display_name,
                "reference_sha1": checksum,
                "reference_size": file_size,
                "reference_filename": Path(
                    media.archive_member or media.source_path
                ).name,
                "reference_source": media.source_path,
                "reference_member": media.archive_member,
                "match_method": match_method if candidates else None,
                "candidates": candidates,
            }

            in_album_candidates = [
                candidate
                for candidate in candidates
                if candidate["asset_id"] in existing_ids
            ]
            if len(candidates) == 1 or len(in_album_candidates) == 1:
                # Prefer the asset already in the canonical album. This makes
                # the first outcome deterministic even if the same bytes were
                # imported into more than one configured user.
                candidate = (
                    in_album_candidates[0]
                    if in_album_candidates
                    else candidates[0]
                )
                base_item.update(
                    {
                        "user": candidate["user"],
                        "asset_id": candidate["asset_id"],
                        "asset_filename": candidate["filename"],
                        "status": (
                            "already_in_album"
                            if candidate["asset_id"] in existing_ids
                            else "matched"
                        ),
                    }
                )
                if candidate["asset_id"] in existing_ids:
                    counts["already_in_album"] += 1
                elif match_method == "sha1":
                    counts["matched_exact"] += 1
                else:
                    counts["matched_filename_size"] += 1
            elif len(candidates) == 0:
                base_item["status"] = "external_missing"
                counts["external_missing"] += 1
            else:
                base_item["status"] = "ambiguous"
                counts["ambiguous"] += 1
            items.append(base_item)

        print(
            f"[PLAN] completed album={album_name!r} "
            f"media={counts['reference_media']} "
            f"exact={counts['matched_exact']} "
            f"fallback={counts['matched_filename_size']} "
            f"already_in_album={counts['already_in_album']} "
            f"external={counts['external_missing']} "
            f"ambiguous={counts['ambiguous']} "
            f"elapsed={time.monotonic() - started:.1f}s",
            flush=True,
        )

        plan["albums"].append(
            {
                "name": album_name,
                "id": album_id,
                "owner": owner,
                "existing_asset_count": len(existing_ids),
                "references": references,
                "counts": counts,
                "items": items,
            }
        )

    validation_errors: list[str] = []
    if missing_albums:
        details = "\n".join(
            f"  - {missing['name']!r} (owner: {missing['owner']})"
            for missing in missing_albums
        )
        validation_errors.append(
            "Albums not found "
            f"({len(missing_albums)}):\n{details}"
        )
    if missing_references:
        details = "\n".join(
            f"  - {missing['reference']} "
            f"(album: {missing['album']!r}, owner: {missing['owner']})"
            for missing in missing_references
        )
        validation_errors.append(
            "Reference paths not found "
            f"({len(missing_references)}):\n{details}"
        )
    if validation_errors:
        plan["partial"] = True
        plan["validation_errors"] = {
            "albums_not_found": missing_albums,
            "reference_paths_not_found": missing_references,
            "messages": validation_errors,
        }

    return plan


def print_plan_summary(plan: dict[str, Any]) -> None:
    print("\nPlan summary:")
    for album in plan["albums"]:
        counts = album["counts"]
        print(f"  {album['name']} ({album['owner']}):")
        print(f"    reference media:       {counts['reference_media']}")
        print(f"    exact matches:         {counts['matched_exact']}")
        print(f"    filename+size matches: {counts['matched_filename_size']}")
        print(f"    already in album:      {counts['already_in_album']}")
        print(f"    external missing:      {counts['external_missing']}")
        print(f"    ambiguous:             {counts['ambiguous']}")

    external_missing = [
        item
        for album in plan["albums"]
        for item in album["items"]
        if item["status"] == "external_missing"
    ]
    ambiguous = [
        item
        for album in plan["albums"]
        for item in album["items"]
        if item["status"] == "ambiguous"
    ]
    if external_missing:
        print("\nExternal upload candidates (first 20):")
        for item in external_missing[:20]:
            print(f"  *** EXTERNAL UPLOAD CANDIDATE *** {item['reference']}")
    if ambiguous:
        print("\nAmbiguous reference files (first 20):")
        for item in ambiguous[:20]:
            candidates = ", ".join(
                f"{candidate['user']}:{candidate['asset_id']}"
                for candidate in item["candidates"]
            )
            print(f"  {item['reference']} -> {candidates}")


def batch(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def safe_component(value: str) -> str:
    """Create a predictable directory component for generated staging files."""

    component = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return component or "album"


def materialize_reference(item: dict[str, Any], destination: Path) -> None:
    """Copy one reference file or ZIP member and verify its expected bytes."""

    source = Path(item["reference_source"])
    if not source.is_file():
        fail(f"Reference source is missing: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.part")
    try:
        if item.get("reference_member") is None:
            with source.open("rb") as source_handle, partial.open("wb") as destination_handle:
                shutil.copyfileobj(source_handle, destination_handle, length=1024 * 1024)
        else:
            try:
                archive = zipfile.ZipFile(source)
            except zipfile.BadZipFile as error:
                fail(f"Invalid ZIP archive {source}: {error}")
            try:
                try:
                    info = archive.getinfo(item["reference_member"])
                except KeyError:
                    fail(
                        f"Reference ZIP member is missing: {source}:{item['reference_member']}"
                    )
                with archive.open(info, "r") as source_handle, partial.open("wb") as destination_handle:
                    shutil.copyfileobj(source_handle, destination_handle, length=1024 * 1024)
            finally:
                archive.close()

        expected_size = int(item["reference_size"])
        if partial.stat().st_size != expected_size:
            fail(
                f"Staged size mismatch for {item['reference']}: "
                f"expected {expected_size}, got {partial.stat().st_size}"
            )
        actual_checksum = sha1_file(partial)
        if actual_checksum != item["reference_sha1"]:
            fail(
                f"Staged SHA-1 mismatch for {item['reference']}: "
                f"expected {item['reference_sha1']}, got {actual_checksum}"
            )
        os.replace(partial, destination)
        copy_reference_sidecar(item, destination.parent)
    finally:
        if partial.exists():
            partial.unlink()


def sidecar_member_candidates(member: str) -> list[str]:
    """Return common Google Takeout sidecar names for a ZIP media member."""

    candidates = [
        f"{member}.supplemental-metadata.json",
        f"{member}.json",
    ]
    if "." in Path(member).name:
        candidates.append(f"{member.rsplit('.', 1)[0]}.json")
    return list(dict.fromkeys(candidates))


def sidecar_path_candidates(media_path: Path) -> list[Path]:
    """Return common sidecar names for a local media file."""

    return list(
        dict.fromkeys(
            [
                Path(f"{media_path}.supplemental-metadata.json"),
                Path(f"{media_path}.json"),
                media_path.with_suffix(".json"),
            ]
        )
    )


def copy_reference_sidecar(
    item: dict[str, Any],
    destination_dir: Path,
) -> Path | None:
    """Copy a matching Google Takeout JSON sidecar when one exists."""

    source = Path(item["reference_source"])
    destination_dir.mkdir(parents=True, exist_ok=True)
    member = item.get("reference_member")

    if member is None:
        candidates = sidecar_path_candidates(source)
        candidates_by_name = {
            candidate.name.casefold(): candidate for candidate in candidates
        }
        for source_candidate in source.parent.iterdir():
            candidate = candidates_by_name.get(source_candidate.name.casefold())
            if candidate is None or not source_candidate.is_file():
                continue
            destination = destination_dir / source_candidate.name
            shutil.copy2(source_candidate, destination)
            return destination
        return None

    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as error:
        fail(f"Invalid ZIP archive {source}: {error}")
    try:
        archive_members = {
            info.filename.casefold(): info for info in archive.infolist()
        }
        for sidecar_member in sidecar_member_candidates(str(member)):
            info = archive_members.get(sidecar_member.casefold())
            if info is None:
                continue
            destination = destination_dir / Path(info.filename).name
            partial = destination.with_name(f".{destination.name}.part")
            try:
                with archive.open(info, "r") as source_handle, partial.open(
                    "wb"
                ) as destination_handle:
                    shutil.copyfileobj(
                        source_handle,
                        destination_handle,
                        length=1024 * 1024,
                    )
                os.replace(partial, destination)
            finally:
                if partial.exists():
                    partial.unlink()
            return destination
    finally:
        archive.close()
    return None


def stage_external_items(
    items: list[dict[str, Any]],
    state_dir: Path,
    album: dict[str, Any],
) -> list[tuple[dict[str, Any], Path]]:
    staging_dir = (
        state_dir
        / "external-upload-staging"
        / safe_component(str(album["owner"]))
        / safe_component(str(album["id"]))
        / "takeout-with-sidecars"
    )
    staged: list[tuple[dict[str, Any], Path]] = []
    for item in items:
        item_dir = staging_dir / safe_component(
            f"{item['reference_sha1']}-{item['reference_filename']}"
        )
        destination = item_dir / item["reference_filename"]
        if destination.exists():
            if (
                destination.stat().st_size != int(item["reference_size"])
                or sha1_file(destination) != item["reference_sha1"]
            ):
                fail(f"Existing staging file failed verification: {destination}")
            sidecar = copy_reference_sidecar(item, destination.parent)
        else:
            materialize_reference(item, destination)
            sidecar = next(
                (
                    candidate
                    for candidate in destination.parent.iterdir()
                    if candidate.name != destination.name
                    and candidate.suffix.casefold() == ".json"
                ),
                None,
            )
        staged.append((item, destination))
        sidecar_text = str(sidecar) if sidecar else "none found"
        print(
            f"[STAGE] {item['reference']} -> {destination} "
            f"sidecar={sidecar_text}"
        )
    return staged


def refresh_user_inventory(
    config: dict[str, Any],
    user: User,
    inventory: Inventory,
    server: str,
) -> None:
    page_size = int(config.get("page_size", DEFAULT_PAGE_SIZE))
    timeout_seconds = int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    retries = int(config.get("retries", DEFAULT_RETRIES))
    started = time.monotonic()
    print(f"[INVENTORY] refreshing after upload user={user.name}", flush=True)
    client = ImmichClient(server, user.api_key, timeout_seconds, retries)
    assets = client.get_all_assets(page_size, user.name)
    inventory.replace_user(user.name, assets)
    print(
        f"[INVENTORY] completed after-upload refresh user={user.name} "
        f"cached={len(assets)} elapsed={time.monotonic() - started:.1f}s",
        flush=True,
    )


def refresh_user_inventory_targeted(
    config: dict[str, Any],
    user: User,
    inventory: Inventory,
    server: str,
    created_after: str,
) -> None:
    """Merge only assets created after a timestamp into the inventory cache."""

    page_size = int(config.get("page_size", DEFAULT_PAGE_SIZE))
    timeout_seconds = int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    retries = int(config.get("retries", DEFAULT_RETRIES))
    started = time.monotonic()
    print(
        f"[INVENTORY] targeted refresh user={user.name} "
        f"created_after={created_after}",
        flush=True,
    )
    client = ImmichClient(server, user.api_key, timeout_seconds, retries)
    assets = client.get_assets_created_after(page_size, created_after, user.name)
    inventory.upsert_user(user.name, assets)
    print(
        f"[INVENTORY] completed targeted refresh user={user.name} "
        f"found={len(assets)} elapsed={time.monotonic() - started:.1f}s",
        flush=True,
    )


def refresh_user_inventory_targeted_checksums(
    config: dict[str, Any],
    user: User,
    inventory: Inventory,
    server: str,
    checksums: Iterable[str],
) -> None:
    """Merge exact checksum lookups for items absent from the time-window scan."""

    checksum_list = sorted(set(checksums))
    if not checksum_list:
        return
    page_size = int(config.get("page_size", DEFAULT_PAGE_SIZE))
    timeout_seconds = int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    retries = int(config.get("retries", DEFAULT_RETRIES))
    started = time.monotonic()
    print(
        f"[INVENTORY] targeted checksum refresh user={user.name} "
        f"checksums={len(checksum_list)}",
        flush=True,
    )
    client = ImmichClient(server, user.api_key, timeout_seconds, retries)
    assets = client.get_assets_by_checksums(
        page_size,
        checksum_list,
        user.name,
    )
    inventory.upsert_user(user.name, assets)
    print(
        f"[INVENTORY] completed targeted checksum refresh user={user.name} "
        f"found={len(assets)} elapsed={time.monotonic() - started:.1f}s",
        flush=True,
    )


def item_candidates(
    inventory: Inventory,
    item: dict[str, Any],
    allow_filename_size: bool,
) -> list[dict[str, Any]]:
    candidates = inventory.by_checksum(item["reference_sha1"])
    if not candidates and allow_filename_size:
        candidates = inventory.by_filename_size(
            item["reference_filename"], int(item["reference_size"])
        )
    return candidates


def upload_with_immich_go(
    config: dict[str, Any],
    owner: User,
    upload_path: Path,
    media_count: int,
) -> int:
    executable = Path(str(config.get("immich_go_path", "/immich-go/immich-go")))
    if not executable.is_file():
        fail(
            f"immich-go executable not found at {executable}; "
            "set immich_go_path in the configuration"
        )
    if not os.access(executable, os.X_OK):
        fail(f"immich-go executable is not executable: {executable}")

    pause_jobs = bool(config.get("immich_go_pause_immich_jobs", False))
    concurrent_tasks = int(config.get("immich_go_concurrent_tasks", 1))
    if not 1 <= concurrent_tasks <= 20:
        fail("immich_go_concurrent_tasks must be between 1 and 20")
    client_timeout = str(config.get("immich_go_client_timeout", "20m"))
    on_errors = str(config.get("immich_go_on_errors", "continue")).casefold()
    if on_errors not in {"stop", "continue"}:
        fail("immich_go_on_errors must be either 'stop' or 'continue'")
    command = [
        str(executable),
        "upload",
        "from-folder",
        "--server",
        str(config["server"]).rstrip("/"),
        "--api-key",
        owner.api_key,
        "--no-ui",
        f"--pause-immich-jobs={'true' if pause_jobs else 'false'}",
        f"--concurrent-tasks={concurrent_tasks}",
        f"--client-timeout={client_timeout}",
        f"--on-errors={on_errors}",
        str(upload_path),
    ]
    print(
        f"[UPLOAD] Uploading {media_count} missing reference media under "
        f"{owner.name} with immich-go..."
    )
    try:
        completed = subprocess.run(command, check=False)
    except OSError as error:
        fail(f"Could not start immich-go at {executable}: {error}")
    return completed.returncode


def append_event(
    journal: dict[str, Any],
    album_log: dict[str, Any],
    journal_path: Path,
    event: dict[str, Any],
) -> None:
    event["at"] = utc_now()
    album_log["events"].append(event)
    write_json_atomic(journal_path, journal)


def apply_existing_items(
    plan: dict[str, Any],
    album: dict[str, Any],
    album_log: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    user_map: dict[str, User],
    items: list[dict[str, Any]],
    known_ids: set[str],
    timeout_seconds: int,
    retries: int,
    batch_size: int,
    page_size: int,
    highlight_external: bool = False,
) -> None:
    """Add resolved assets and verify each batch through the album search API."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        asset_id = item["asset_id"]
        if asset_id in known_ids:
            print(
                f"[SKIP] already in album | album={album['name']} | "
                f"reference={item['reference']} | asset={asset_id}"
            )
            append_event(
                journal,
                album_log,
                journal_path,
                {
                    "outcome": "already_in_album",
                    "reference": item["reference"],
                    "asset_id": asset_id,
                    "source_user": item.get("user"),
                },
            )
            continue
        grouped.setdefault(item["user"], []).append(item)

    for user_name, user_items in grouped.items():
        user = user_map.get(user_name)
        if user is None:
            fail(f"Plan references unknown user {user_name}")
        unique_items: dict[str, dict[str, Any]] = {}
        for item in user_items:
            unique_items.setdefault(item["asset_id"], item)
        pending_items = [
            item for asset_id, item in unique_items.items() if asset_id not in known_ids
        ]
        if not pending_items:
            continue
        source_client = ImmichClient(
            plan["server"],
            user.api_key,
            timeout_seconds,
            retries,
        )
        print(
            f"[ATTACH] Adding {len(pending_items)} assets from {user_name} to "
            f"{album['name']!r} via {user_name}'s key "
            f"(album owner {album['owner']})..."
        )
        for item_batch in batch(pending_items, batch_size):
            asset_batch = [item["asset_id"] for item in item_batch]
            # Immich checks AssetShare against the authenticated user's asset
            # ownership or partner access. Therefore the source user must add
            # their own existing assets, and must be an album editor.
            add_result = source_client.add_assets(album["id"], asset_batch)
            verified_ids = source_client.get_album_asset_ids(album["id"], page_size)
            actually_added = [asset_id for asset_id in asset_batch if asset_id in verified_ids]
            if len(actually_added) != len(asset_batch):
                missing = sorted(set(asset_batch) - set(actually_added))
                response_errors = []
                if isinstance(add_result, list):
                    response_errors = [
                        {
                            "id": result.get("id"),
                            "error": result.get("error"),
                        }
                        for result in add_result
                        if isinstance(result, dict) and result.get("success") is False
                    ]
                fail(
                    f"Immich did not confirm all assets in batch for {user_name}; "
                    f"missing {len(missing)} IDs. API errors: {response_errors or 'not provided'}. "
                    "Journal is safe to resume."
                )

            album_log["changes"].append(
                {
                    "user": user_name,
                    "asset_ids": actually_added,
                    "reason": "external_upload" if highlight_external else "existing_asset",
                    "applied_at": utc_now(),
                }
            )
            known_ids.update(actually_added)
            for item in item_batch:
                if item["asset_id"] not in actually_added:
                    continue
                if highlight_external:
                    print(
                        f"*** EXTERNAL UPLOAD *** | uploaded under {album['owner']} "
                        f"and added to album={album['name']} | "
                        f"reference={item['reference']} | asset={item['asset_id']}"
                    )
                    outcome = "uploaded_external"
                else:
                    print(
                        f"[ATTACH] existing asset | source_user={user_name} | "
                        f"album={album['name']} | reference={item['reference']} | "
                        f"asset={item['asset_id']}"
                    )
                    outcome = "attached_existing"
                append_event(
                    journal,
                    album_log,
                    journal_path,
                    {
                        "outcome": outcome,
                        "reference": item["reference"],
                        "asset_id": item["asset_id"],
                        "source_user": user_name,
                        "album_owner": album["owner"],
                    },
                )
            write_json_atomic(journal_path, journal)


def apply_album_members(
    owner_client: ImmichClient,
    album: dict[str, Any],
    album_log: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    member_names: list[str],
    member_role: str,
    user_ids: dict[str, str],
) -> None:
    """Share an album and ensure configured members have the required role."""

    if not member_names:
        return

    current_album = owner_client.get_album(album["id"])
    member_roles = album_user_roles(current_album)
    owner_id = user_ids.get(album["owner"])
    if owner_id is None:
        fail(f"Could not resolve Immich user ID for album owner {album['owner']}")

    for member_name in member_names:
        member_id = user_ids.get(member_name)
        if member_id is None:
            fail(f"Could not resolve Immich user ID for album member {member_name}")
        if member_id == owner_id:
            print(
                f"[MEMBERS] owner | album={album['name']} | user={member_name}",
                flush=True,
            )
            continue

        current_role = member_roles.get(member_id)
        role_satisfies_requirement = (
            current_role == member_role
            or (member_role == "viewer" and current_role == "editor")
        )
        if role_satisfies_requirement:
            print(
                f"[MEMBERS] already shared | album={album['name']} | "
                f"user={member_name} | role={current_role}",
                flush=True,
            )
            continue

        action = "adding" if current_role is None else "updating"
        print(
            f"[MEMBERS] {action} | album={album['name']} | "
            f"user={member_name} | role={member_role}"
            + (f" (was {current_role})" if current_role else ""),
            flush=True,
        )
        owner_client.add_user(album["id"], member_id, member_role)
        updated_album = owner_client.get_album(album["id"])
        updated_roles = album_user_roles(updated_album)
        if updated_roles.get(member_id) != member_role:
            fail(
                f"Immich did not confirm member {member_name!r} has role "
                f"{member_role!r} on album {album['name']!r}"
            )
        member_roles[member_id] = member_role
        membership_event = {
            "user": member_name,
            "user_id": member_id,
            "role": member_role,
            "previous_role": current_role,
        }
        if current_role is None:
            membership_event["added_at"] = utc_now()
            album_log["members_added"].append(membership_event)
        else:
            membership_event["updated_at"] = utc_now()
            album_log.setdefault("members_updated", []).append(membership_event)
        write_json_atomic(journal_path, journal)


def apply_plan(
    config: dict[str, Any],
    users: list[User],
    plan: dict[str, Any],
    journal_path: Path,
    upload_missing: bool,
    allow_fallback: bool,
    refresh_all_users: bool = False,
) -> dict[str, Any]:
    if plan.get("server") != str(config["server"]).rstrip("/"):
        fail("Plan server does not match configuration server")
    if plan.get("tool_version") != TOOL_VERSION:
        fail("Plan was created by an incompatible tool version")

    user_map = users_by_name(users)
    timeout_seconds = int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    retries = int(config.get("retries", DEFAULT_RETRIES))
    batch_size = int(config.get("batch_size", DEFAULT_BATCH_SIZE))
    page_size = int(config.get("page_size", DEFAULT_PAGE_SIZE))
    state_dir = Path(config.get("state_dir", "./reconciler-state"))
    inventory = Inventory(state_dir / "inventory.sqlite3")
    refresh_scope = str(
        config.get("apply_inventory_refresh", "album_owner")
    ).casefold()
    if refresh_scope not in {"album_owner", "all", "targeted"}:
        fail(
            "apply_inventory_refresh must be 'album_owner', 'all', or 'targeted'"
        )
    if refresh_all_users:
        refresh_scope = "all"
    member_names, member_role = album_member_config(
        config,
        user_map.keys(),
    )
    allow_filename_size = bool(
        (config.get("match") if isinstance(config.get("match"), dict) else {}).get(
            "allow_filename_size", True
        )
    ) and allow_fallback
    upload_failures_found = False
    apply_started_at = utc_now()
    journal: dict[str, Any] = {
        "tool": "google_album_reconciler",
        "tool_version": TOOL_VERSION,
        "started_at": apply_started_at,
        "server": plan["server"],
        "inventory_refresh": refresh_scope,
        "status": "running",
        "albums": [],
    }
    write_json_atomic(journal_path, journal)

    try:
        user_ids = (
            resolve_user_ids(users, plan["server"], timeout_seconds, retries)
            if member_names
            else {}
        )
        for album in plan["albums"]:
            album_id = album["id"]
            album_log = {
                "name": album["name"],
                "id": album_id,
                "owner": album["owner"],
                "events": [],
                "changes": [],
                "uploads": [],
                "upload_failures": [],
                "members_added": [],
            }
            journal["albums"].append(album_log)
            owner = album["owner"]
            owner_client = ImmichClient(
                plan["server"], user_map[owner].api_key, timeout_seconds, retries
            )
            known_ids = owner_client.get_album_asset_ids(album_id, page_size)
            write_json_atomic(journal_path, journal)

            apply_album_members(
                owner_client,
                album,
                album_log,
                journal,
                journal_path,
                member_names,
                member_role,
                user_ids,
            )

            existing_items = [
                item
                for item in album["items"]
                if item.get("status") in {"matched", "already_in_album"}
            ]
            external_items = [
                item for item in album["items"] if item.get("status") == "external_missing"
            ]

            # A plan can be older than the apply run. Refresh before uploading so
            # a newly imported owner asset is attached instead of duplicated.
            # An item explicitly marked force_external_upload is an exception:
            # it must remain an external upload even if another user's existing
            # asset has the same checksum.
            if external_items:
                recheck_external = [
                    item
                    for item in external_items
                    if not item.get("force_external_upload")
                ]
                still_external: list[dict[str, Any]] = [
                    item
                    for item in external_items
                    if item.get("force_external_upload")
                ]
                if recheck_external and refresh_scope == "targeted":
                    plan_created_at = plan.get("created_at")
                    created_after = (
                        plan_created_at
                        if isinstance(plan_created_at, str) and plan_created_at
                        else apply_started_at
                    )
                    refresh_user_inventory_targeted(
                        config,
                        user_map[owner],
                        inventory,
                        plan["server"],
                        created_after,
                    )
                elif recheck_external and refresh_scope == "all":
                    print(
                        "[INVENTORY] apply refresh scope=all users",
                        flush=True,
                    )
                    refresh_inventory(config, users, inventory, plan["server"])
                elif recheck_external:
                    print(
                        f"[INVENTORY] apply refresh scope=album owner ({owner}); "
                        "other users use the plan cache",
                        flush=True,
                    )
                    refresh_user_inventory(
                        config,
                        user_map[owner],
                        inventory,
                        plan["server"],
                    )
                for item in recheck_external:
                    candidates = item_candidates(inventory, item, allow_filename_size)
                    if len(candidates) > 1:
                        fail(
                            f"Reference {item['reference']!r} now matches multiple assets; "
                            "refusing to guess."
                        )
                    if len(candidates) == 1:
                        candidate = candidates[0]
                        resolved = dict(item)
                        resolved.update(
                            {
                                "user": candidate["user"],
                                "asset_id": candidate["asset_id"],
                                "asset_filename": candidate["filename"],
                            }
                        )
                        existing_items.append(resolved)
                    else:
                        still_external.append(item)
                external_items = still_external

            apply_existing_items(
                plan,
                album,
                album_log,
                journal,
                journal_path,
                user_map,
                existing_items,
                known_ids,
                timeout_seconds,
                retries,
                batch_size,
                page_size,
            )

            if external_items:
                if not upload_missing:
                    fail(
                        f"Album {album['name']!r} still has {len(external_items)} "
                        "reference media with no matching Immich asset."
                    )
                staged = stage_external_items(external_items, state_dir, album)
                staging_dir = staged[0][1].parent.parent
                upload_window_start = utc_now_minus(60)
                return_code = upload_with_immich_go(
                    config, user_map[owner], staging_dir, len(staged)
                )
                if refresh_scope == "targeted":
                    refresh_user_inventory_targeted(
                        config,
                        user_map[owner],
                        inventory,
                        plan["server"],
                        upload_window_start,
                    )
                else:
                    refresh_user_inventory(
                        config,
                        user_map[owner],
                        inventory,
                        plan["server"],
                    )
                if return_code != 0:
                    print(
                        f"WARNING: immich-go exited with code {return_code}; "
                        "checking each staged file and continuing with verified uploads."
                    )

                unresolved_after_window = []
                for item, staged_path in staged:
                    candidates = item_candidates(inventory, item, False)
                    owner_candidates = [
                        candidate
                        for candidate in candidates
                        if candidate["user"] == owner
                    ]
                    if not owner_candidates:
                        unresolved_after_window.append(item)

                if unresolved_after_window and refresh_scope == "targeted":
                    refresh_user_inventory_targeted_checksums(
                        config,
                        user_map[owner],
                        inventory,
                        plan["server"],
                        (item["reference_sha1"] for item in unresolved_after_window),
                    )

                uploaded_items: list[dict[str, Any]] = []
                for item, staged_path in staged:
                    candidates = item_candidates(inventory, item, False)
                    owner_candidates = [
                        candidate
                        for candidate in candidates
                        if candidate["user"] == owner
                    ]
                    if len(owner_candidates) != 1:
                        upload_failures_found = True
                        failure = {
                            "user": owner,
                            "reference": item["reference"],
                            "sha1": item["reference_sha1"],
                            "staged_path": str(staged_path),
                            "matches_under_owner": len(owner_candidates),
                            "immich_go_returncode": return_code,
                            "failed_at": utc_now(),
                        }
                        album_log["upload_failures"].append(failure)
                        print(
                            f"ERROR: upload not verified; skipping album membership | "
                            f"reference={item['reference']} | "
                            f"matches_under_{owner}={len(owner_candidates)} | "
                            f"staged={staged_path}",
                            flush=True,
                        )
                        continue
                    candidate = owner_candidates[0]
                    resolved = dict(item)
                    resolved.update(
                        {
                            "user": owner,
                            "asset_id": candidate["asset_id"],
                            "asset_filename": candidate["filename"],
                        }
                    )
                    album_log["uploads"].append(
                        {
                            "user": owner,
                            "asset_id": candidate["asset_id"],
                            "reference": item["reference"],
                            "sha1": item["reference_sha1"],
                            "immich_go_returncode": return_code,
                            "uploaded_at": utc_now(),
                        }
                    )
                    uploaded_items.append(resolved)
                write_json_atomic(journal_path, journal)
                apply_existing_items(
                    plan,
                    album,
                    album_log,
                    journal,
                    journal_path,
                    user_map,
                    uploaded_items,
                    known_ids,
                    timeout_seconds,
                    retries,
                    batch_size,
                    page_size,
                    highlight_external=True,
                )

        journal["status"] = "partial" if upload_failures_found else "completed"
        journal["completed_at"] = utc_now()
        if upload_failures_found:
            journal["warning"] = (
                "Some external uploads were not verified and were not added to albums. "
                "Retry the same plan with a new journal after reviewing upload failures."
            )
        write_json_atomic(journal_path, journal)
        return journal
    except Exception:
        journal["status"] = "partial"
        journal["failed_at"] = utc_now()
        write_json_atomic(journal_path, journal)
        raise


def rollback_journal(
    config: dict[str, Any],
    users: list[User],
    journal: dict[str, Any],
) -> None:
    if journal.get("server") != str(config["server"]).rstrip("/"):
        fail("Journal server does not match configuration server")
    user_map = users_by_name(users)
    timeout_seconds = int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    retries = int(config.get("retries", DEFAULT_RETRIES))

    for album in journal.get("albums", []):
        album_id = album["id"]
        for change in album.get("changes", []):
            user_name = change["user"]
            user = user_map.get(user_name)
            if user is None:
                fail(f"Journal references unknown user {user_name}")
            client = ImmichClient(journal["server"], user.api_key, timeout_seconds, retries)
            current_ids = client.get_album_asset_ids(
                album_id, int(config.get("page_size", DEFAULT_PAGE_SIZE))
            )
            to_remove = [asset_id for asset_id in change["asset_ids"] if asset_id in current_ids]
            if not to_remove:
                continue
            print(f"Rolling back {len(to_remove)} assets from {album['name']!r} via {user_name}...")
            for asset_batch in batch(to_remove, int(config.get("batch_size", DEFAULT_BATCH_SIZE))):
                client.remove_assets(album_id, asset_batch)

        owner = album["owner"]
        owner_user = user_map.get(owner)
        if owner_user is None:
            fail(f"Journal references unknown album owner {owner}")
        client = ImmichClient(journal["server"], owner_user.api_key, timeout_seconds, retries)
        for member in reversed(album.get("members_added", [])):
            user_id = member.get("user_id")
            if not isinstance(user_id, str) or not user_id:
                fail(
                    f"Journal entry for album {album['name']!r} has an invalid "
                    "member user ID"
                )
            client.remove_user(album_id, user_id)
            print(
                f"[ROLLBACK] removed member {member.get('user', user_id)} "
                f"from {album['name']!r}",
                flush=True,
            )


def require_confirmation(message: str, confirmed: bool) -> None:
    if not confirmed:
        fail(f"{message} Re-run with --yes after reviewing the plan/journal.")


def command_plan(args: argparse.Namespace) -> None:
    config, users = load_config(Path(args.config))
    state_dir = Path(config.get("state_dir", "./reconciler-state"))
    inventory_path = state_dir / "inventory.sqlite3"
    plan_path = Path(args.output or state_dir / "plan.json")
    if args.rebuild_inventory:
        archive_inventory_cache(inventory_path)
    inventory_exists = inventory_path.exists()
    inventory = Inventory(inventory_path)
    if args.rebuild_inventory or args.refresh_inventory or not inventory_exists:
        refresh_inventory(config, users, inventory, str(config["server"]).rstrip("/"))
    else:
        print(f"Using cached inventory: {inventory_path}")
    plan = build_plan(config, users, inventory)
    write_json_atomic(plan_path, plan)
    print_plan_summary(plan)
    print(f"\nPlan written to: {plan_path}")
    if plan.get("validation_errors"):
        print(
            "WARNING: The plan was written with validation errors and is not "
            "safe to apply until they are resolved.",
            flush=True,
        )
        fail(
            "Configuration validation failed after checking all configured albums; "
            f"partial plan preserved at {plan_path}"
        )
    print("No Immich album changes were made.")


def command_apply(args: argparse.Namespace) -> None:
    config, users = load_config(Path(args.config))
    plan = read_json(Path(args.plan))
    print_plan_summary(plan)
    external_count = 0
    for album in plan.get("albums", []):
        counts = album["counts"]
        external_count += counts.get("external_missing", 0)
        if counts["ambiguous"]:
            fail(
                f"Plan for {album['name']!r} contains ambiguous files. "
                "Resolve them before apply; the script will not guess."
            )
        fallback_items = [
            item
            for item in album["items"]
            if item.get("status") == "matched" and item.get("match_method") != "sha1"
        ]
        if fallback_items and not args.allow_fallback:
            fail(
                f"Plan for {album['name']!r} contains {len(fallback_items)} "
                "filename+size fallback matches. Review the plan and re-run apply "
                "with --allow-fallback if those matches are acceptable."
            )
    if external_count and not args.upload_missing:
        fail(
            f"The plan contains {external_count} reference media not found in any "
            "configured Immich user. Review them, then re-run apply with "
            "--upload-missing to upload them under their album owners."
        )
    if plan.get("validation_errors"):
        fail(
            "The plan was written with validation errors and cannot be applied. "
            "Fix the configuration/reference paths, or create an explicitly "
            "filtered plan containing only validated albums."
        )
    confirmation = "This will add assets to existing Immich albums."
    if args.upload_missing:
        confirmation += " Missing reference media will be uploaded under album owners."
    require_confirmation(
        confirmation,
        args.yes,
    )
    journal_path = Path(args.journal)
    if journal_path.exists() and not args.overwrite_journal:
        fail(f"Journal already exists: {journal_path}. Choose another path or use --overwrite-journal.")
    journal = apply_plan(
        config,
        users,
        plan,
        journal_path,
        upload_missing=args.upload_missing,
        allow_fallback=args.allow_fallback,
        refresh_all_users=args.refresh_all_users,
    )
    print(f"Apply completed. Journal written to: {journal_path}")
    print(f"Status: {journal['status']}")


def command_rollback(args: argparse.Namespace) -> None:
    config, users = load_config(Path(args.config))
    journal_path = Path(args.journal)
    journal = read_json(journal_path)
    require_confirmation(
        "This will remove only album memberships recorded in this journal.",
        args.yes,
    )
    rollback_journal(config, users, journal)
    journal["status"] = "rolled_back"
    journal["rolled_back_at"] = utc_now()
    write_json_atomic(journal_path, journal)
    print(f"Rollback completed for recorded changes in: {journal_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="Build a read-only matching plan")
    plan.add_argument("--config", required=True, help="Configuration JSON path")
    plan.add_argument("--output", help="Plan JSON output path")
    plan.add_argument(
        "--refresh-inventory",
        action="store_true",
        help="Refresh the Immich asset inventory instead of using the SQLite cache",
    )
    plan.add_argument(
        "--rebuild-inventory",
        action="store_true",
        help=(
            "Archive the existing SQLite inventory cache and build a fresh one; "
            "implies --refresh-inventory"
        ),
    )
    plan.set_defaults(function=command_plan)

    apply = subparsers.add_parser("apply", help="Apply a reviewed plan")
    apply.add_argument("--config", required=True, help="Configuration JSON path")
    apply.add_argument("--plan", required=True, help="Plan JSON created by plan")
    apply.add_argument("--journal", required=True, help="Journal path for rollback")
    apply.add_argument("--yes", action="store_true", help="Confirm the write operation")
    apply.add_argument(
        "--allow-fallback",
        action="store_true",
        help="Allow unique filename+size matches in addition to exact SHA-1 matches",
    )
    apply.add_argument(
        "--upload-missing",
        action="store_true",
        help=(
            "Upload reference media not found in any configured Immich user "
            "under the album owner"
        ),
    )
    apply.add_argument(
        "--refresh-all-users",
        action="store_true",
        help=(
            "Refresh every configured user during apply before uploading missing "
            "media; otherwise only the album owner is refreshed"
        ),
    )
    apply.add_argument(
        "--overwrite-journal",
        action="store_true",
        help="Allow replacing an existing journal path",
    )
    apply.set_defaults(function=command_apply)

    rollback = subparsers.add_parser("rollback", help="Remove memberships recorded by apply")
    rollback.add_argument("--config", required=True, help="Configuration JSON path")
    rollback.add_argument("--journal", required=True, help="Journal created by apply")
    rollback.add_argument("--yes", action="store_true", help="Confirm the rollback")
    rollback.set_defaults(function=command_rollback)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.function(args)
        return 0
    except ReconcileError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
