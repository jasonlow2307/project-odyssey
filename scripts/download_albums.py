"""Download Google Photos albums visible to the signed-in account as ZIP files."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import time
import unicodedata
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterator, Literal
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, BrowserContext, CDPSession, Locator, Page, Playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


CDP_URL = os.getenv("CDP_URL", "http://127.0.0.1:9222")
ALBUMS_URL = os.getenv("ALBUMS_URL", "https://photos.google.com/albums")
ACCOUNT_LABEL = os.getenv("ACCOUNT_LABEL", "account-1")
MAX_ALBUMS = int(os.getenv("MAX_ALBUMS", "0"))
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "5")))
ALBUM_RETRIES = max(1, int(os.getenv("ALBUM_RETRIES", "3")))
NAVIGATION_RETRIES = max(1, int(os.getenv("NAVIGATION_RETRIES", "3")))
ALBUM_TITLE_TIMEOUT_SECONDS = max(5, int(os.getenv("ALBUM_TITLE_TIMEOUT_SECONDS", "120")))
DOWNLOAD_EVENT_TIMEOUT_MS = max(30_000, int(os.getenv("DOWNLOAD_EVENT_TIMEOUT_MS", "900000")))
DOWNLOAD_COMPLETION_TIMEOUT_SECONDS = max(60, int(os.getenv("DOWNLOAD_COMPLETION_TIMEOUT_SECONDS", "21600")))
DOWNLOAD_PROGRESS_INTERVAL_SECONDS = max(10, int(os.getenv("DOWNLOAD_PROGRESS_INTERVAL_SECONDS", "60")))
VERIFY_ZIP_CRC = os.getenv("VERIFY_ZIP_CRC", "true").strip().lower() in {"1", "true", "yes", "on"}
SKIP_ALBUMS_TEXT = os.getenv("SKIP_ALBUMS", "")
DOWNLOAD_STATUS_SELECTOR = "div.zyTWof-gIZMF#c9, div.zyTWof-gIZMF"
DOWNLOAD_ROOT = Path("/downloads/Google Photos")
TEMP_ROOT = Path(os.getenv("TMPDIR", "/downloads/.playwright-tmp"))
LOGIN_TIMEOUT_SECONDS = 10 * 60
ALBUM_PATH_RE = re.compile(r"/(?:album|share)/([^/?#]+)")
GENERIC_TITLES = {"google photos", "photos", "albums", "shared album", "album", "more options"}
NON_ALBUM_TITLE_FRAGMENTS = (
    "press question mark to see shortcut keys available",
    "press question mark to see keyboard shortcuts",
    "shortcut keys available",
)


@dataclass(frozen=True)
class Album:
    album_id: str
    url: str
    title_hint: str


@dataclass(frozen=True)
class DownloadStarted:
    guid: str
    frame_id: str
    suggested_filename: str


class CDPDownloadMonitor:
    """Track Chromium's native download progress and completion events."""

    def __init__(self, session: CDPSession) -> None:
        self.session = session
        self.loop = asyncio.get_running_loop()
        self.start_waiters: dict[str, asyncio.Future[dict[str, object]]] = {}
        self.completion_waiters: dict[str, asyncio.Future[dict[str, object]]] = {}
        self.latest_progress: dict[str, dict[str, object]] = {}
        session.on("Browser.downloadWillBegin", self._on_download_will_begin)
        session.on("Browser.downloadProgress", self._on_download_progress)

    def register_start(self, frame_ids: list[str]) -> asyncio.Future[dict[str, object]]:
        future: asyncio.Future[dict[str, object]] = self.loop.create_future()
        for frame_id in frame_ids:
            self.start_waiters[frame_id] = future
        return future

    def unregister_start(self, future: asyncio.Future[dict[str, object]], cancel: bool = False) -> None:
        for frame_id, waiting in list(self.start_waiters.items()):
            if waiting is future:
                del self.start_waiters[frame_id]
        if cancel and not future.done():
            future.cancel()

    def _on_download_will_begin(self, event: dict[str, object]) -> None:
        frame_id = str(event.get("frameId", ""))
        future = self.start_waiters.get(frame_id)
        if future is None or future.done():
            return

        self.unregister_start(future)
        guid = str(event.get("guid", ""))
        if not guid:
            future.set_exception(RuntimeError("Chromium started a download without a GUID."))
            return

        self.completion_waiters[guid] = self.loop.create_future()
        future.set_result(event)

    def _on_download_progress(self, event: dict[str, object]) -> None:
        guid = str(event.get("guid", ""))
        if guid not in self.completion_waiters:
            return
        self.latest_progress[guid] = event
        if event.get("state") in {"completed", "canceled"}:
            future = self.completion_waiters[guid]
            if not future.done():
                future.set_result(event)

    async def wait_for_completion(self, download: DownloadStarted, album_title: str) -> dict[str, object]:
        future = self.completion_waiters.get(download.guid)
        if future is None:
            raise RuntimeError(f"No Chromium completion listener exists for download {download.guid}.")

        started = time.monotonic()
        try:
            while not future.done():
                elapsed = time.monotonic() - started
                remaining = DOWNLOAD_COMPLETION_TIMEOUT_SECONDS - elapsed
                if remaining <= 0:
                    try:
                        await self.session.send("Browser.cancelDownload", {"guid": download.guid})
                    except Exception:
                        pass
                    raise TimeoutError(
                        f"Chromium did not complete the download of '{album_title}' "
                        f"within {DOWNLOAD_COMPLETION_TIMEOUT_SECONDS} seconds."
                    )

                interval = min(DOWNLOAD_PROGRESS_INTERVAL_SECONDS, remaining)
                done, _ = await asyncio.wait({future}, timeout=interval)
                if done:
                    break

                progress = self.latest_progress.get(download.guid, {})
                received = progress.get("receivedBytes", 0)
                total = progress.get("totalBytes", 0)
                if isinstance(received, (int, float)) and isinstance(total, (int, float)) and total > 0:
                    logging.info(
                        "Chromium download progress for '%s': %.1f%% (%d/%d bytes; %.1f minutes elapsed).",
                        album_title,
                        min(100.0, received * 100.0 / total),
                        received,
                        total,
                        elapsed / 60,
                    )
                else:
                    logging.info(
                        "Chromium is still downloading '%s' (%.1f minutes elapsed).",
                        album_title,
                        elapsed / 60,
                    )

            result = await future
            state = result.get("state")
            if state != "completed":
                raise RuntimeError(f"Chromium canceled the download of '{album_title}' (state={state}).")
            return result
        finally:
            self.completion_waiters.pop(download.guid, None)
            self.latest_progress.pop(download.guid, None)


async def page_frame_ids(page: Page) -> list[str]:
    """Return all frame IDs for a page so browser-level download events can be matched safely."""
    session = await page.context.new_cdp_session(page)
    try:
        result = await session.send("Page.getFrameTree")
    finally:
        await session.detach()

    frame_ids: list[str] = []

    def collect(node: dict[str, object]) -> None:
        frame = node.get("frame")
        if isinstance(frame, dict) and isinstance(frame.get("id"), str):
            frame_ids.append(frame["id"])
        children = node.get("childFrames", [])
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    collect(child)

    frame_tree = result.get("frameTree")
    if isinstance(frame_tree, dict):
        collect(frame_tree)
    if not frame_ids:
        raise RuntimeError("Chromium returned no frame IDs for the album page.")
    return frame_ids


def clean_album_title(value: str) -> str:
    """Remove Google Photos count and toolbar text surrounding an album title."""
    title = re.sub(r"\s+", " ", value).strip(" :-\u2013\u2014")
    title = re.sub(r"[\s_]+more options\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"(?:^|[\s_])\d[\d,]*\s+(?:items?|photos?|videos?)\b.*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"[\s_]*[\u00b7\u2022][\s_]*(?:shared(?: album)?|google photos)\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*[-\u2013\u2014]\s*google photos\s*$", "", title, flags=re.IGNORECASE)
    parts = re.split(r"\s*[\u00b7\u2022]\s*", title, maxsplit=1)
    if len(parts) == 2:
        date_metadata = re.compile(
            r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
            r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
            r"mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|"
            r"sat(?:urday)?|sun(?:day)?)\b",
            re.IGNORECASE,
        )
        if re.search(r"\d", parts[1]) and date_metadata.search(parts[1]):
            title = parts[0]
    return title.strip(" :-\u2013\u2014")


def normalize_album_title(value: str) -> str:
    """Normalize case, metadata, whitespace, apostrophes, and emoji presentation."""
    normalized = unicodedata.normalize("NFC", value)
    normalized = normalized.replace("’", "'").replace("‘", "'").replace("\ufe0e", "").replace("\ufe0f", "")
    return re.sub(r"\s+", " ", clean_album_title(normalized)).strip().casefold()


SKIP_ALBUMS = {
    normalize_album_title(name)
    for name in SKIP_ALBUMS_TEXT.split("|")
    if name.strip()
}
JobResult = Literal["downloaded", "configured_skip", "renamed", "already_present", "failed"]


class AlbumActionUnavailableError(RuntimeError):
    """The album's own menu did not offer an explicit Download all action."""


def safe_name(value: str, fallback: str) -> str:
    """Return a filename-safe component while keeping readable album names."""
    cleaned = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", value).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:100] or fallback


def readable_title(candidates: list[str], album_id: str) -> str:
    """Choose a plausible album name from accessible text and nearby card text."""
    for candidate in candidates:
        for line in candidate.splitlines():
            title = clean_album_title(line)
            folded_title = title.casefold()
            if (
                not title
                or folded_title in GENERIC_TITLES
                or any(fragment in folded_title for fragment in NON_ALBUM_TITLE_FRAGMENTS)
                or title == album_id
            ):
                continue
            if album_id in title and not re.search(r"[A-Za-z ]", title.replace(album_id, "")):
                continue
            if len(title) > 120:
                continue
            return title
    return ""


def is_skipped_album(title: str, title_hint: str) -> bool:
    if not SKIP_ALBUMS:
        return False
    return any(normalize_album_title(candidate) in SKIP_ALBUMS for candidate in (title, title_hint))


def log_manifest(path: Path, record: dict[str, object]) -> None:
    record["timestamp"] = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as manifest:
        manifest.write(json.dumps(record, ensure_ascii=False) + "\n")
        manifest.flush()
        os.fsync(manifest.fileno())


class ManifestWriter:
    """Serialize durable manifest writes from concurrent workers."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()

    async def write(self, record: dict[str, object]) -> None:
        async with self.lock:
            await asyncio.to_thread(log_manifest, self.path, record)


class AlbumIndex:
    """Maintain an atomic, de-duplicated album-title-to-ZIP index."""

    HEADER = "Google Photos album name\tZIP filename\n"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()

    async def ensure_file(self) -> None:
        async with self.lock:
            await asyncio.to_thread(self._ensure_file_sync)

    async def record(self, album_title: str, archive: Path) -> None:
        async with self.lock:
            await asyncio.to_thread(self._record_sync, album_title, archive.name)

    def _ensure_file_sync(self) -> None:
        self._write_sync(self._read_entries_sync())

    def _record_sync(self, album_title: str, filename: str) -> None:
        entries = self._read_entries_sync()
        title = re.sub(r"\s+", " ", clean_album_title(album_title)).replace("\t", " ").strip()
        entries[self._entry_key(filename)] = (title, filename)
        self._write_sync(entries)

    def _read_entries_sync(self) -> dict[str, tuple[str, str]]:
        entries: dict[str, tuple[str, str]] = {}
        if not self.path.exists():
            return entries
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line or line == self.HEADER.rstrip("\n"):
                continue
            title, separator, filename = line.partition("\t")
            if separator and filename and (self.path.parent / filename).is_file():
                entries[self._entry_key(filename)] = (title, filename)
        return entries

    def _write_sync(self, entries: dict[str, tuple[str, str]]) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="") as index_file:
            index_file.write(self.HEADER)
            for saved_title, saved_filename in entries.values():
                index_file.write(f"{saved_title}\t{saved_filename}\n")
            index_file.flush()
            os.fsync(index_file.fileno())
        os.replace(temporary, self.path)

    @staticmethod
    def _entry_key(filename: str) -> str:
        stem = Path(filename).stem
        return stem.rsplit("--", 1)[-1] if "--" in stem else stem


async def record_album_archive(index: AlbumIndex, title: str, archive: Path) -> None:
    try:
        await index.record(title, archive)
    except Exception as error:
        # Keep a verified ZIP even if the convenience index cannot be updated.
        logging.error("Could not update albums.txt for '%s': %s", archive.name, error)


@contextmanager
def account_lock(account_dir: Path) -> Iterator[None]:
    """Prevent two runners from writing the same account folder at once."""
    lock_path = account_dir / ".downloader.lock"
    with lock_path.open("a", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"Another downloader is already using {account_dir}.") from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class BrowserSession:
    """Share Chromium's signed-in context while giving each job its own tab."""

    def __init__(self, playwright: Playwright) -> None:
        self.playwright = playwright
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.browser_cdp: CDPSession | None = None
        self.download_monitor: CDPDownloadMonitor | None = None
        self.connection_lock = asyncio.Lock()

    async def _connect_locked(self) -> None:
        deadline = time.monotonic() + 90
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                browser = await self.playwright.chromium.connect_over_cdp(
                    CDP_URL, timeout=5_000, is_local=True
                )
                if not browser.contexts:
                    raise RuntimeError("Chromium exposed no browser context over CDP.")
                browser_cdp = await browser.new_browser_cdp_session()
                await browser_cdp.send(
                    "Browser.setDownloadBehavior",
                    {
                        "behavior": "allowAndName",
                        "downloadPath": str(TEMP_ROOT),
                        "eventsEnabled": True,
                    },
                )
                self.browser = browser
                self.context = browser.contexts[0]
                self.browser_cdp = browser_cdp
                self.download_monitor = CDPDownloadMonitor(browser_cdp)
                logging.info(
                    "Chromium downloads will be written to the shared staging folder: %s",
                    TEMP_ROOT,
                )
                return
            except Exception as error:
                last_error = error
                await asyncio.sleep(2)
        raise RuntimeError(f"Could not connect to Chromium at {CDP_URL}: {last_error}")

    async def ensure_connected(self) -> None:
        async with self.connection_lock:
            if self.context is None:
                await self._connect_locked()

    async def reconnect(self) -> None:
        async with self.connection_lock:
            self.browser = None
            self.context = None
            self.browser_cdp = None
            self.download_monitor = None
            await self._connect_locked()

    async def new_page(self) -> Page:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            async with self.connection_lock:
                try:
                    if self.context is None:
                        await self._connect_locked()
                    assert self.context is not None
                    return await self.context.new_page()
                except Exception as error:
                    last_error = error
                    self.browser = None
                    self.context = None
                    self.browser_cdp = None
                    self.download_monitor = None
            if attempt < 3:
                logging.warning("Could not create a Chromium tab (%d/3): %s", attempt, last_error)
                await asyncio.sleep(2)
        raise RuntimeError(f"Could not create a Chromium tab: {last_error}")

    async def login_page(self) -> Page:
        await self.ensure_connected()
        async with self.connection_lock:
            try:
                assert self.context is not None
                pages = [page for page in self.context.pages if not page.is_closed()]
                photos_pages = [page for page in pages if urlparse(page.url).hostname == "photos.google.com"]
                if photos_pages:
                    return photos_pages[-1]
                if pages:
                    return pages[0]
                return await self.context.new_page()
            except Exception:
                self.browser = None
                self.context = None
        await self.ensure_connected()
        return await self.new_page()


async def goto_with_retry(page: Page, url: str, description: str) -> None:
    """Navigate on document commit; retry transient browser and network errors."""
    last_error: Exception | None = None
    for attempt in range(1, NAVIGATION_RETRIES + 1):
        try:
            response = await page.goto(url, wait_until="commit", timeout=60_000)
            if response and response.status >= 400:
                raise RuntimeError(f"{description} returned HTTP {response.status}.")
            return
        except PlaywrightTimeoutError as error:
            if (urlparse(page.url).hostname or "") == "photos.google.com":
                logging.warning("%s navigation timed out after reaching Google Photos; continuing.", description)
                return
            last_error = error
        except Exception as error:
            last_error = error

        if attempt < NAVIGATION_RETRIES:
            delay = min(2 ** (attempt - 1), 8)
            logging.warning(
                "%s navigation failed (%d/%d): %s; retrying in %ds.",
                description,
                attempt,
                NAVIGATION_RETRIES,
                last_error,
                delay,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"Could not open {description} after {NAVIGATION_RETRIES} attempts: {last_error}")


async def sign_in_visible(page: Page) -> bool:
    for role in ("button", "link"):
        try:
            locator = page.get_by_role(role, name=re.compile(r"^sign in$", re.IGNORECASE))
            for index in range(min(await locator.count(), 5)):
                if await locator.nth(index).is_visible():
                    return True
        except Exception:
            continue
    return False


async def login_required(page: Page) -> bool:
    host = urlparse(page.url).hostname or ""
    return host == "accounts.google.com" or (host == "photos.google.com" and await sign_in_visible(page))


async def wait_for_manual_sign_in(session: BrowserSession, page: Page) -> Page:
    logging.info("Opening Google Photos. Sign into the intended account in the Chromium desktop if prompted.")
    try:
        await goto_with_retry(page, ALBUMS_URL, "Google Photos albums")
    except Exception as error:
        logging.warning("Initial Google Photos navigation failed: %s", error)

    deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
    last_navigation = time.monotonic()
    last_notice = 0.0
    while time.monotonic() < deadline:
        try:
            if page.is_closed():
                raise RuntimeError("The Chromium tab was closed.")
            host = urlparse(page.url).hostname or ""
            if host == "photos.google.com" and not await sign_in_visible(page):
                await page.wait_for_timeout(2_000)
                if (urlparse(page.url).hostname or "") == "photos.google.com" and not await sign_in_visible(page):
                    logging.info("Google Photos is open. Starting album discovery.")
                    return page

            now = time.monotonic()
            if now - last_navigation >= 20:
                try:
                    await goto_with_retry(page, ALBUMS_URL, "Google Photos albums")
                except Exception as error:
                    logging.warning("Google Photos is not ready yet: %s", error)
                last_navigation = time.monotonic()
        except Exception as error:
            logging.warning("Chromium page became unavailable while waiting for sign-in: %s", error)
            try:
                await session.reconnect()
                page = await session.login_page()
            except Exception as reconnect_error:
                logging.warning("Could not reconnect to Chromium while waiting for sign-in: %s", reconnect_error)
                await asyncio.sleep(5)
            last_navigation = 0.0

        now = time.monotonic()
        if now - last_notice >= 30:
            logging.info("Waiting for the intended Google account to finish signing in in Chromium.")
            last_notice = now
        await asyncio.sleep(1)
    raise TimeoutError("Timed out waiting for Google sign-in to finish in the Chromium desktop.")


async def scroll_album_view(page: Page) -> None:
    """Advance both the pointer's scroll target and the document where possible."""
    try:
        viewport = page.viewport_size or {"width": 1200, "height": 800}
        await page.mouse.move(viewport["width"] // 2, viewport["height"] * 3 // 4)
        await page.mouse.wheel(0, max(800, viewport["height"]))
        await page.evaluate("window.scrollBy(0, Math.max(800, window.innerHeight))")
    except Exception as error:
        logging.debug("Album list scroll attempt was interrupted: %s", error)


async def scan_album_links(page: Page) -> tuple[list[Album], bool]:
    """Scroll the album page and collect links and nearby visible title text."""
    found: dict[str, Album] = {}
    stable_rounds = 0
    read_errors = 0
    selector = "a[href*='/album/'], a[href*='/share/']"

    for _ in range(100):
        try:
            anchors = await page.locator(selector).evaluate_all(
                """elements => elements.map(anchor => {
                    const titles = [];
                    const add = value => {
                        if (value && value.trim() && !titles.includes(value.trim())) titles.push(value.trim());
                    };
                    add(anchor.getAttribute('aria-label'));
                    add(anchor.getAttribute('title'));
                    add(anchor.innerText);
                    add(anchor.textContent);
                    let node = anchor.parentElement;
                    for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) add(node.innerText);
                    return {href: anchor.href, titles};
                })"""
            )
        except Exception as error:
            logging.warning("Could not read the album list during this scroll pass: %s", error)
            read_errors += 1
            if read_errors >= 5:
                return list(found.values()), False
            try:
                await scroll_album_view(page)
                await page.wait_for_timeout(1_000)
            except Exception:
                return list(found.values()), False
            continue

        read_errors = 0
        for anchor in anchors:
            href = anchor.get("href")
            if not href:
                continue
            absolute_url = urljoin("https://photos.google.com", href)
            parsed = urlparse(absolute_url)
            match = ALBUM_PATH_RE.search(parsed.path)
            if parsed.hostname != "photos.google.com" or not match:
                continue
            album_id = match.group(1)
            if album_id not in found:
                title_hint = readable_title(anchor.get("titles", []), album_id) or album_id
                found[album_id] = Album(album_id=album_id, url=absolute_url, title_hint=title_hint)

        previous_count = len(found)
        await scroll_album_view(page)
        await page.wait_for_timeout(700)
        if len(found) == previous_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
        if stable_rounds >= 8:
            break
    return list(found.values()), True


async def album_links(page: Page) -> list[Album]:
    """Retry discovery when the page initially renders without album links."""
    combined: dict[str, Album] = {}
    for attempt in range(1, 4):
        found, completed = await scan_album_links(page)
        combined.update({album.album_id: album for album in found})
        if completed and combined:
            return list(combined.values())
        if attempt < 3:
            logging.warning(
                "Album discovery pass %d/3 was %s; reloading the album page.",
                attempt,
                "incomplete" if not completed else "empty",
            )
            await goto_with_retry(page, ALBUMS_URL, "Google Photos albums")
            await page.wait_for_timeout(3_000)
    return list(combined.values())


async def discover_albums(session: BrowserSession, page: Page) -> tuple[Page, list[Album]]:
    """Reconnect and retry if Chromium drops the page during album discovery."""
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            albums = await album_links(page)
            if albums:
                return page, albums
            last_error = None
            logging.warning("No album links were found on discovery pass %d/3.", attempt)
        except Exception as error:
            last_error = error
            logging.warning("Album discovery failed (%d/3): %s", attempt, error)
        if attempt < 3:
            try:
                page = await session.new_page()
                await goto_with_retry(page, ALBUMS_URL, "Google Photos albums")
                await page.wait_for_timeout(2_000)
            except Exception as reconnect_error:
                last_error = reconnect_error
                logging.warning("Could not restore the album page; retrying discovery: %s", reconnect_error)
                await asyncio.sleep(5)
    if last_error is not None:
        raise RuntimeError(f"Album discovery could not complete after three attempts: {last_error}")
    return page, []


async def visible_download_action(page: Page) -> Locator | None:
    action_pattern = re.compile(r"^\s*download all\s*$", re.IGNORECASE)
    try:
        menus = page.get_by_role("menu")
        for menu_index in range(await menus.count()):
            menu = menus.nth(menu_index)
            if not await menu.is_visible():
                continue
            actions = menu.get_by_role("menuitem", name=action_pattern)
            for action_index in range(await actions.count()):
                action = actions.nth(action_index)
                if await action.is_visible():
                    return action

        # Some Google Photos menu versions omit role="menu" on the container.
        actions = page.get_by_role("menuitem", name=action_pattern)
        for action_index in range(await actions.count()):
            action = actions.nth(action_index)
            if await action.is_visible():
                return action
    except Exception as error:
        logging.debug("Could not inspect the open album menu: %s", error)
    return None


async def scoped_more_options(anchor: Locator) -> Locator | None:
    """Find one More options button inside a title anchor's nearby container."""
    more_pattern = re.compile(r"^\s*more options\s*$", re.IGNORECASE)
    container = anchor
    for _ in range(8):
        container = container.locator("xpath=..")
        buttons = container.get_by_role("button", name=more_pattern)
        visible_buttons: list[Locator] = []
        for index in range(await buttons.count()):
            button = buttons.nth(index)
            if await button.is_visible():
                visible_buttons.append(button)
        if len(visible_buttons) == 1:
            return visible_buttons[0]
        if len(visible_buttons) > 1:
            return None
    return None


async def find_album_options_button(page: Page, album_title: str) -> Locator:
    """Wait for the album's data-title header and scope its own More options button."""
    title_attributes = page.locator("[data-title]")
    headings = page.locator("h1, h2, h3, [role='heading']")
    wanted_title = normalize_album_title(album_title)
    deadline = time.monotonic() + ALBUM_TITLE_TIMEOUT_SECONDS

    while True:
        title_anchors: list[Locator] = []
        for index in range(await title_attributes.count()):
            anchor = title_attributes.nth(index)
            if not await anchor.is_visible():
                continue
            try:
                data_title = await anchor.get_attribute("data-title") or ""
            except Exception:
                continue
            if normalize_album_title(data_title) == wanted_title:
                title_anchors.append(anchor)

        # Google Photos album headers are div[data-title] rather than semantic
        # headings. Keep semantic headings only as a fallback for UI variants.
        anchors = title_anchors
        if not anchors:
            for index in range(await headings.count()):
                heading = headings.nth(index)
                if not await heading.is_visible():
                    continue
                try:
                    lines = (await heading.inner_text()).splitlines()
                except Exception:
                    continue
                if any(normalize_album_title(line) == wanted_title for line in lines):
                    anchors.append(heading)

        candidates: dict[str, Locator] = {}
        for anchor in anchors:
            button = await scoped_more_options(anchor)
            if button is None:
                continue
            try:
                key = await button.evaluate(
                    """element => {
                        const rect = element.getBoundingClientRect();
                        return `${element.getAttribute('data-tooltip-id') || ''}:`
                            + `${Math.round(rect.x)}:${Math.round(rect.y)}:`
                            + `${Math.round(rect.width)}:${Math.round(rect.height)}`;
                    }"""
                )
            except Exception:
                key = str(len(candidates))
            candidates[key] = button

        if len(candidates) == 1:
            return next(iter(candidates.values()))
        if len(candidates) > 1:
            raise RuntimeError(
                f"Found multiple More options buttons scoped to the visible title '{album_title}'; "
                "refusing to guess."
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"No visible data-title or heading with a uniquely scoped More options button was found "
                f"for '{album_title}' within {ALBUM_TITLE_TIMEOUT_SECONDS} seconds; refusing to click any button."
            )
        await page.wait_for_timeout(1_000)


async def find_download_all(page: Page, album_title: str) -> Locator:
    """Open only the album header menu and require its exact Download all action."""
    options_button = await find_album_options_button(page, album_title)
    logging.info("Opening album-header options for '%s'.", album_title)
    await options_button.click(timeout=15_000)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        action = await visible_download_action(page)
        if action:
            return action
        await page.wait_for_timeout(250)

    try:
        menu_text = await page.get_by_role("menu").last.inner_text(timeout=1_000)
        logging.info("Album options for '%s' showed: %s", album_title, menu_text[:300].replace("\n", " | "))
    except Exception:
        pass
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    raise AlbumActionUnavailableError(
        f"The album-header menu for '{album_title}' has no exact 'Download all' action; "
        "no download was started and no other menu was clicked."
    )


def output_path_for(account_dir: Path, album: Album, title: str) -> Path:
    readable = safe_name(title, f"Album-{album.album_id[-8:]}")
    suffix = safe_name(album.album_id, "album")[-12:]
    return account_dir / f"{readable}--{suffix}.zip"


def expected_output_path(account_dir: Path, album: Album) -> Path:
    title = readable_title([album.title_hint], album.album_id)
    if not title:
        title = f"Album-{safe_name(album.album_id, 'album')[-8:]}"
    return output_path_for(account_dir, album, title)


def title_for_existing_archive(album: Album, archive: Path) -> str:
    title = readable_title([album.title_hint], album.album_id)
    if title:
        return title

    suffix = f"--{safe_name(album.album_id, 'album')[-12:]}.zip"
    filename_title = archive.name[:-len(suffix)] if archive.name.endswith(suffix) else archive.stem
    return readable_title([filename_title], album.album_id) or filename_title


def legacy_output_path(account_dir: Path, album: Album) -> Path:
    """Path used by the earlier script when it had no readable album title."""
    old_title = safe_name(album.album_id, "album")
    suffix = safe_name(album.album_id, "album")[-12:]
    return account_dir / f"{old_title}--{suffix}.zip"


def valid_zip(path: Path, verify_crc: bool = False) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 22:
            return False
        with zipfile.ZipFile(path) as archive:
            archive.infolist()
            return not verify_crc or archive.testzip() is None
    except (OSError, zipfile.BadZipFile, EOFError, RuntimeError, NotImplementedError, zlib.error):
        return False


async def existing_archive(account_dir: Path, album: Album) -> Path | None:
    suffix = f"--{safe_name(album.album_id, 'album')[-12:]}.zip"
    candidates = list(account_dir.glob(f"*{suffix}"))
    legacy = legacy_output_path(account_dir, album)
    candidates.sort(key=lambda path: (path != legacy, path.name))
    for candidate in candidates:
        if await asyncio.to_thread(valid_zip, candidate, False):
            return candidate
    return None


async def resolve_album_title(page: Page, album: Album) -> str:
    # The album-list card is a stronger source than arbitrary page headings,
    # which can include Google Photos help overlays and keyboard-shortcut UI.
    list_title = readable_title([album.title_hint], album.album_id)
    if list_title:
        return list_title

    deadline = time.monotonic() + ALBUM_TITLE_TIMEOUT_SECONDS
    while True:
        candidates: list[str] = []
        try:
            candidates.extend(
                await page.locator("h1, h2, h3, [role='heading']").evaluate_all(
                    "elements => elements.filter(element => element.checkVisibility())"
                    ".map(element => element.innerText || element.textContent || '')"
                )
            )
        except Exception:
            pass
        try:
            meta_title = await page.locator("meta[property='og:title']").get_attribute("content")
            if meta_title:
                candidates.append(meta_title)
        except Exception:
            pass
        try:
            candidates.append(await page.title())
        except Exception:
            pass
        name = readable_title(candidates, album.album_id)
        if name:
            return name
        if time.monotonic() >= deadline:
            return ""
        try:
            await page.wait_for_timeout(1_000)
        except Exception:
            return ""


async def wait_for_stable_download_file(path: Path, album_title: str) -> None:
    """Wait until Chromium's completed file is present and its size has settled."""
    deadline = time.monotonic() + 120
    previous_size = -1
    while time.monotonic() < deadline:
        try:
            if path.is_file():
                current_size = path.stat().st_size
                if current_size > 0 and current_size == previous_size:
                    return
                previous_size = current_size
        except OSError:
            pass
        await asyncio.sleep(1)
    raise TimeoutError(f"Chromium reported completion, but its file for '{album_title}' did not settle at {path}.")


async def save_download_with_progress(
    monitor: CDPDownloadMonitor,
    download: DownloadStarted,
    path: Path,
    album_title: str,
) -> None:
    """Wait for Chromium's native completed event, then move its shared-volume file."""
    result = await monitor.wait_for_completion(download, album_title)
    chromium_path = result.get("filePath")
    source = Path(str(chromium_path)) if isinstance(chromium_path, str) and chromium_path else TEMP_ROOT / download.guid
    await wait_for_stable_download_file(source, album_title)
    os.replace(source, path)
    logging.info("Chromium marked '%s' complete; moved its file to %s.", album_title, path)


async def visible_download_statuses(page: Page) -> list[str] | None:
    """Read visible Google Photos download status text, or None if the page is unavailable."""
    try:
        return await page.locator(DOWNLOAD_STATUS_SELECTOR).evaluate_all(
            "elements => elements.filter(element => element.checkVisibility())"
            ".map(element => (element.innerText || element.textContent || '').trim())"
        )
    except Exception:
        return None


def is_preparing_download_status(status: str) -> bool:
    return bool(re.match(r"^preparing to download(?:\s|$)", re.sub(r"\s+", " ", status).strip(), re.IGNORECASE))


def status_is_for_album(status: str, album_title: str) -> bool:
    normalized_status = re.sub(r"\s+", " ", status).strip()
    album_text = re.sub(r"^preparing to download\s+", "", normalized_status, flags=re.IGNORECASE)
    return normalize_album_title(album_title) in normalize_album_title(album_text)


async def wait_for_download_start(
    page: Page,
    album_title: str,
    monitor: CDPDownloadMonitor,
    start_event: asyncio.Future[dict[str, object]],
) -> DownloadStarted:
    """Observe Google's preparation toast and await Chromium's native download-start event."""
    preparation_seen = False
    preparation_transition_logged = False
    deadline = time.monotonic() + DOWNLOAD_EVENT_TIMEOUT_MS / 1_000
    try:
        while not start_event.done():
            statuses = await visible_download_statuses(page)
            if statuses is not None:
                preparing_statuses = [status for status in statuses if is_preparing_download_status(status)]
                matching_status = next(
                    (status for status in preparing_statuses if status_is_for_album(status, album_title)),
                    None,
                )
                if matching_status and not preparation_seen:
                    preparation_seen = True
                    logging.info("Google Photos is preparing the ZIP for '%s': %s", album_title, matching_status)
                elif preparation_seen and not preparing_statuses and not preparation_transition_logged:
                    preparation_transition_logged = True
                    logging.info(
                        "Google Photos left the 'Preparing to download' state for '%s'; waiting for Chromium's download event.",
                        album_title,
                    )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Chromium did not start the download of '{album_title}' "
                    f"within {DOWNLOAD_EVENT_TIMEOUT_MS / 1_000:.0f} seconds."
                )
            await asyncio.wait({start_event}, timeout=min(0.5, remaining))

        event = await start_event
        download = DownloadStarted(
            guid=str(event["guid"]),
            frame_id=str(event.get("frameId", "")),
            suggested_filename=str(event.get("suggestedFilename", "")),
        )
        logging.info(
            "Chromium CDP reported download start for '%s' (file '%s', id %s).",
            album_title,
            download.suggested_filename,
            download.guid,
        )
        return download
    finally:
        monitor.unregister_start(start_event, cancel=True)


async def maybe_restore_sign_in(
    session: BrowserSession,
    page: Page,
    auth_lock: asyncio.Lock,
) -> Page:
    try:
        if not await login_required(page):
            return page
    except Exception:
        return page
    async with auth_lock:
        try:
            if await login_required(page):
                logging.warning("Google sign-in is required again; waiting for the intended account.")
                page = await wait_for_manual_sign_in(session, page)
        except Exception as error:
            logging.warning("Could not restore the Google sign-in session: %s", error)
    return page


def page_is_open(page: Page) -> bool:
    try:
        return not page.is_closed()
    except Exception:
        return False


async def close_page(page: Page | None) -> None:
    if page is not None and page_is_open(page):
        try:
            await page.close()
        except Exception:
            pass


async def rename_legacy_archive(
    session: BrowserSession,
    account_dir: Path,
    manifest: ManifestWriter,
    album: Album,
    archive: Path,
    auth_lock: asyncio.Lock,
    album_index: AlbumIndex,
) -> JobResult:
    page: Page | None = None
    last_error: Exception | None = None
    for attempt in range(1, ALBUM_RETRIES + 1):
        try:
            page = await session.new_page()
            await goto_with_retry(page, album.url, f"album '{album.title_hint}'")
            await page.wait_for_timeout(2_000)
            title = await resolve_album_title(page, album)
            if not title:
                if SKIP_ALBUMS:
                    raise RuntimeError(
                        "Could not resolve the album title while a skip list is active; "
                        "refusing to rename or interact with its menus."
                    )
                title = f"Album-{safe_name(album.album_id, 'album')[-8:]}"
            if is_skipped_album(title, album.title_hint):
                logging.info("Configured skip: leaving existing archive unchanged for '%s'.", title)
                await record_album_archive(album_index, title, archive)
                await manifest.write(
                    {"album_id": album.album_id, "title": title, "url": album.url, "status": "skipped_by_name", "file": str(archive)}
                )
                return "configured_skip"
            output = output_path_for(account_dir, album, title)
            result: JobResult = "renamed"
            status = "renamed"
            if output != archive:
                if output.exists() and await asyncio.to_thread(valid_zip, output, False):
                    logging.info("Readable album-named ZIP already exists; leaving the older copy in place: %s", output.name)
                    status = "already_named"
                    result = "already_present"
                else:
                    os.replace(archive, output)
                    logging.info("Renamed existing ZIP to album name: %s", output.name)
            else:
                result = "already_present"
                status = "already_named"
            await record_album_archive(album_index, title, output)
            await manifest.write(
                {"album_id": album.album_id, "title": title, "url": album.url, "status": status, "file": str(output)}
            )
            return result
        except Exception as error:
            last_error = error
            logging.warning("Could not rename the existing album ZIP (%d/%d): %s", attempt, ALBUM_RETRIES, error)
            if page is not None:
                page = await maybe_restore_sign_in(session, page, auth_lock)
            if attempt < ALBUM_RETRIES:
                await asyncio.sleep(min(2 ** attempt * 2, 20))
        finally:
            await close_page(page)
    await manifest.write(
        {"album_id": album.album_id, "title": album.title_hint, "url": album.url, "status": "rename_failed", "error": str(last_error)}
    )
    return "failed"


async def download_album(
    session: BrowserSession,
    account_dir: Path,
    manifest: ManifestWriter,
    album: Album,
    auth_lock: asyncio.Lock,
    album_index: AlbumIndex,
) -> JobResult:
    last_error: Exception | None = None
    screenshot: Path | None = None
    for attempt in range(1, ALBUM_RETRIES + 1):
        page: Page | None = None
        partial: Path | None = None
        try:
            page = await session.new_page()
            await goto_with_retry(page, album.url, f"album '{album.title_hint}'")
            await page.wait_for_timeout(2_500)
            title = await resolve_album_title(page, album)
            if not title:
                if SKIP_ALBUMS:
                    raise RuntimeError(
                        "Could not resolve the album title while a skip list is active; "
                        "refusing to open any menu or start a download."
                    )
                title = f"Album-{safe_name(album.album_id, 'album')[-8:]}"
            if is_skipped_album(title, album.title_hint):
                logging.info("Configured skip: not downloading '%s'.", title)
                await manifest.write(
                    {"album_id": album.album_id, "title": title, "url": album.url, "status": "skipped_by_name"}
                )
                return "configured_skip"
            output = output_path_for(account_dir, album, title)
            partial = output.with_name(output.name + ".part")
            partial.unlink(missing_ok=True)

            download_action = await find_download_all(page, title)
            logging.info("Requesting Google Photos ZIP for '%s' (attempt %d/%d).", title, attempt, ALBUM_RETRIES)
            monitor = session.download_monitor
            if monitor is None:
                raise RuntimeError("Chromium's native download monitor is unavailable.")
            start_event = monitor.register_start(await page_frame_ids(page))
            start_waiter = asyncio.create_task(wait_for_download_start(page, title, monitor, start_event))
            try:
                await download_action.click(timeout=10_000)
                download = await start_waiter
            except Exception:
                if not start_waiter.done():
                    start_waiter.cancel()
                await asyncio.gather(start_waiter, return_exceptions=True)
                raise
            await save_download_with_progress(monitor, download, partial, title)

            is_valid = await asyncio.to_thread(valid_zip, partial, VERIFY_ZIP_CRC)
            if not is_valid:
                raise RuntimeError("Google Photos download did not produce a valid ZIP archive.")
            os.replace(partial, output)
            await record_album_archive(album_index, title, output)
            await manifest.write(
                {
                    "album_id": album.album_id,
                    "title": title,
                    "url": album.url,
                    "status": "downloaded",
                    "file": str(output),
                    "attempts": attempt,
                }
            )
            logging.info("Saved and verified album '%s': %s", title, output)
            return "downloaded"
        except AlbumActionUnavailableError as error:
            last_error = error
            await manifest.write(
                {
                    "album_id": album.album_id,
                    "title": album.title_hint,
                    "url": album.url,
                    "status": "download_unavailable",
                    "attempts": attempt,
                    "error": str(error),
                }
            )
            logging.error("No album download action is available for '%s': %s", album.title_hint, error)
            return "failed"
        except Exception as error:
            last_error = error
            if partial is not None:
                try:
                    partial.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    logging.warning("Could not remove incomplete download %s: %s", partial, cleanup_error)
            logging.warning(
                "Album '%s' failed (%d/%d): %s",
                album.title_hint,
                attempt,
                ALBUM_RETRIES,
                error,
            )
            if page is not None:
                page = await maybe_restore_sign_in(session, page, auth_lock)
            if attempt == ALBUM_RETRIES and page is not None and page_is_open(page):
                screenshot = account_dir / f"failed--{safe_name(album.album_id, 'album')[-12:]}.png"
                try:
                    await page.screenshot(path=str(screenshot), full_page=False)
                except Exception:
                    screenshot = None
            if attempt < ALBUM_RETRIES:
                delay = min(2 ** (attempt - 1) * 3, 30)
                logging.info("Retrying album '%s' in %ds.", album.title_hint, delay)
                await asyncio.sleep(delay)
        finally:
            await close_page(page)

    record: dict[str, object] = {
        "album_id": album.album_id,
        "title": album.title_hint,
        "url": album.url,
        "status": "failed",
        "attempts": ALBUM_RETRIES,
        "error": str(last_error),
    }
    if screenshot is not None:
        record["screenshot"] = str(screenshot)
    await manifest.write(record)
    logging.error("Giving up on album '%s' after %d attempts: %s", album.title_hint, ALBUM_RETRIES, last_error)
    return "failed"


async def run_concurrently(
    items: list[object],
    worker_count: int,
    handler: Callable[[object], Awaitable[JobResult]],
) -> list[JobResult]:
    """Keep a rolling number of jobs active; the next job starts when one finishes."""
    semaphore = asyncio.Semaphore(worker_count)

    async def run_one(item: object) -> JobResult:
        async with semaphore:
            try:
                return await handler(item)
            except Exception:
                logging.exception("Unexpected worker failure.")
                return "failed"

    return list(await asyncio.gather(*(run_one(item) for item in items)))


def remove_stale_partials(account_dir: Path) -> None:
    for partial in account_dir.glob("*.zip.part"):
        try:
            partial.unlink()
            logging.info("Removed incomplete download from an earlier run: %s", partial.name)
        except OSError as error:
            logging.warning("Could not remove incomplete file %s: %s", partial, error)


async def run(playwright: Playwright, account_dir: Path, manifest: ManifestWriter) -> tuple[int, int, int]:
    album_index = AlbumIndex(account_dir / "albums.txt")
    await album_index.ensure_file()
    session = BrowserSession(playwright)
    page = await session.login_page()
    page = await wait_for_manual_sign_in(session, page)
    page, albums = await discover_albums(session, page)
    if not albums:
        screenshot = account_dir / "album-list-empty.png"
        try:
            await page.screenshot(path=str(screenshot), full_page=False)
        except Exception:
            pass
        raise RuntimeError(
            "No album links were discovered. Check the signed-in account and album page; "
            f"diagnostic screenshot: {screenshot}"
        )

    logging.info("Discovered %d albums for label '%s'.", len(albums), ACCOUNT_LABEL)
    migrations: list[tuple[Album, Path]] = []
    pending: list[Album] = []
    already_present = 0
    configured_skips = 0
    for album in albums:
        archive = await existing_archive(account_dir, album)
        if is_skipped_album(album.title_hint, album.title_hint):
            configured_skips += 1
            logging.info("Configured skip: album-list title matches '%s'.", album.title_hint)
            if archive is not None:
                await record_album_archive(album_index, title_for_existing_archive(album, archive), archive)
            await manifest.write(
                {"album_id": album.album_id, "title": album.title_hint, "url": album.url, "status": "skipped_by_name"}
            )
            continue
        if archive is None:
            pending.append(album)
        elif archive != expected_output_path(account_dir, album):
            migrations.append((album, archive))
        else:
            already_present += 1
            await record_album_archive(album_index, title_for_existing_archive(album, archive), archive)

    auth_lock = asyncio.Lock()
    migration_jobs = [
        (album, archive)
        for album, archive in migrations
    ]

    async def migrate(item: object) -> JobResult:
        album, archive = item  # type: ignore[misc]
        return await rename_legacy_archive(session, account_dir, manifest, album, archive, auth_lock, album_index)

    migration_results = await run_concurrently(
        migration_jobs,
        MAX_CONCURRENT_DOWNLOADS,
        migrate,
    )
    migration_renamed = sum(result == "renamed" for result in migration_results)
    migration_skipped = sum(result == "configured_skip" for result in migration_results)
    migration_failures = sum(result == "failed" for result in migration_results)
    if migration_jobs:
        logging.info(
            "Renamed %d existing archive(s), skipped %d by name, and failed to rename %d.",
            migration_renamed,
            migration_skipped,
            migration_failures,
        )

    if MAX_ALBUMS > 0:
        pending = pending[:MAX_ALBUMS]
    logging.info(
        "%d album ZIP(s) already present; %d archive(s) to rename; %d album(s) queued with up to %d concurrent downloads.",
        already_present,
        len(migration_jobs),
        len(pending),
        MAX_CONCURRENT_DOWNLOADS,
    )
    await manifest.write(
        {
            "status": "run_started_downloads",
            "account": ACCOUNT_LABEL,
            "discovered": len(albums),
            "already_present": already_present,
            "legacy_to_rename": len(migration_jobs),
            "queued": len(pending),
            "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
        }
    )

    async def download(item: object) -> JobResult:
        return await download_album(session, account_dir, manifest, item, auth_lock, album_index)  # type: ignore[arg-type]

    results = await run_concurrently(pending, MAX_CONCURRENT_DOWNLOADS, download)
    succeeded = sum(result == "downloaded" for result in results)
    configured_skips_from_workers = sum(result == "configured_skip" for result in results)
    failures = sum(result == "failed" for result in results)
    configured_skips += migration_skipped + configured_skips_from_workers
    await manifest.write(
        {
            "status": "run_complete",
            "discovered": len(albums),
            "already_present": already_present,
            "configured_skips": configured_skips,
            "renamed": migration_renamed,
            "rename_failures": migration_failures,
            "downloaded": succeeded,
            "download_failures": failures,
        }
    )
    logging.info(
        "Run complete: %d downloaded, %d already present, %d configured skips, %d archives renamed, %d failures.",
        succeeded,
        already_present,
        configured_skips,
        migration_renamed,
        failures + migration_failures,
    )
    return succeeded, failures, migration_failures


async def async_main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if SKIP_ALBUMS:
        logging.info("Loaded %d normalized album exclusions from SKIP_ALBUMS.", len(SKIP_ALBUMS))
    else:
        logging.warning("SKIP_ALBUMS is empty; no album exclusions are active.")
    logging.info(
        "Google Photos download-start timeout: %.1f minutes.",
        DOWNLOAD_EVENT_TIMEOUT_MS / 60_000,
    )
    logging.info("Album title wait timeout: %d seconds.", ALBUM_TITLE_TIMEOUT_SECONDS)
    logging.info("Album action mode: resolve title before menus; album header only; exact 'Download all' required.")
    if MAX_ALBUMS < 0:
        raise ValueError("MAX_ALBUMS must be 0 (all pending albums) or a positive number.")

    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    account_dir = DOWNLOAD_ROOT / safe_name(ACCOUNT_LABEL, "account-1")
    account_dir.mkdir(parents=True, exist_ok=True)
    manifest = ManifestWriter(account_dir / "manifest.jsonl")

    with account_lock(account_dir):
        remove_stale_partials(account_dir)
        await manifest.write(
            {
                "status": "run_started",
                "account": ACCOUNT_LABEL,
                "max_albums": MAX_ALBUMS,
                "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
            }
        )
        async with async_playwright() as playwright:
            _, failures, migration_failures = await run(playwright, account_dir, manifest)
        if failures or migration_failures:
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(async_main())
