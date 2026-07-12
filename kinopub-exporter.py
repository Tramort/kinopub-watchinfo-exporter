import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import requests_cache
import traceback_with_variables.activate_by_import  # noqa: F401


BASE_URL = os.environ.get("KINOPUB_BASE_URL", "https://api.service-kp.com")
TOKEN_PLACEHOLDER = "YOUR_BEARER_TOKEN_HERE"
TOKEN = os.environ.get("KINOPUB_TOKEN", TOKEN_PLACEHOLDER)
USER_AGENT = "KinoPubWatchInfoExporter/1.0"

CACHE_DB_FILE = Path(".kinopub_http_cache")
CACHE_TTL_HOURS = 1
OUTPUT_DIR = Path("data")
WATCHLIST_OUTPUT_FILE = OUTPUT_DIR / "watchlist.json"
WATCHING_OUTPUT_FILE = OUTPUT_DIR / "watching.json"
HISTORY_OUTPUT_FILE = OUTPUT_DIR / "history.json"
REQUEST_TIMEOUT_SECONDS = 25
MAX_RETRIES = 3
BACKOFF_SECONDS = 1.0
LOG_LEVEL = os.environ.get("KINOPUB_LOG_LEVEL", "INFO")
RAW_DUMP_OUTPUT_DIR = OUTPUT_DIR / "kinopub_raw_dumps"

IMDB_PATTERN = re.compile(r"^tt\d{7,10}$")

MOVIE_TYPES = {"movie", "documovie"}
THREE_D_TYPES = {"3d"}
SHOW_TYPES = {"serial", "docuserial", "tvshow"}

_logged_warning_keys: set[tuple[str, Optional[int], Optional[str]]] = set()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ts_to_iso_utc(timestamp: Any) -> str:
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        ts = time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ts_to_dt_utc(timestamp: Any) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_iso_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def extract_last_history_timestamp(history_file: Path) -> Optional[datetime]:
    """Return latest watched_at timestamp from data/history.json, if available."""
    if not history_file.exists():
        return None
    try:
        with history_file.open("r", encoding="utf-8") as file_handle:
            payload = json.load(file_handle)
    except (json.JSONDecodeError, OSError):
        logging.warning("Could not parse %s; incremental sync timestamp unavailable.", history_file)
        return None

    history_items = payload.get("history") if isinstance(payload, dict) else None
    if not isinstance(history_items, list) or not history_items:
        return None

    latest: Optional[datetime] = None
    for item in history_items:
        if not isinstance(item, dict):
            continue
        parsed = parse_iso_utc(item.get("watched_at"))
        if parsed and (latest is None or parsed > latest):
            latest = parsed
    return latest


def is_valid_imdb_id(value: Any) -> bool:
    return isinstance(value, str) and bool(IMDB_PATTERN.match(value.strip()))


def normalize_imdb_id(value: Any) -> Optional[str]:
    if isinstance(value, (int, float)):
        digits = str(int(value))
        if 5 <= len(digits) <= 10:
            return f"tt{digits.zfill(7)}"
        return None
    elif isinstance(value, str):
        raw = value.strip().lower()
        if not raw:
            return None
        if IMDB_PATTERN.match(raw):
            return raw
        if raw.isdigit() and 5 <= len(raw) <= 10:
            return f"tt{raw.zfill(7)}"
    else:
        logging.warning("Unexpected IMDb ID type: %s (%s)", type(value), value)
    return None


def normalize_kinopoisk_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            return int(raw)
    return None


def normalize_kinopub_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            return int(raw)
    return None


def extract_title_fields(
    item: Dict[str, Any],
    fallback_item: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    fallback = fallback_item if isinstance(fallback_item, dict) else {}

    title = item.get("title") or fallback.get("title")
    original_title = item.get("title_en") or fallback.get("title_en")

    # KinoPub can store combined titles as "Localized / Original".
    if isinstance(title, str):
        title = title.strip() or None
    if isinstance(original_title, str):
        original_title = original_title.strip() or None

    if not original_title and isinstance(title, str) and " / " in title:
        left, right = [part.strip() for part in title.split(" / ", 1)]
        if right and left != right:
            title = left or title
            original_title = right

    if not original_title:
        original_title = title

    year = item.get("year")
    if year is None:
        year = fallback.get("year")

    try:
        normalized_year = int(year) if year is not None else None
    except (TypeError, ValueError):
        normalized_year = None

    return title, original_title, normalized_year


def log_identifier_warning_once(kind: str, kinopoisk_id: Optional[int], title: Optional[str], message: str) -> None:
    key = (kind, kinopoisk_id, title)
    if key in _logged_warning_keys:
        return
    _logged_warning_keys.add(key)
    logging.warning(message)


def warn_missing_metadata_if_needed(
    title: Optional[str],
    original_title: Optional[str],
    year: Optional[int],
    context: str,
) -> None:
    if original_title and year is not None:
        return
    missing = []
    if not original_title:
        missing.append("original_title")
    if year is None:
        missing.append("year")
    logging.warning("%s item missing metadata (%s), title: %s", context, ", ".join(missing), title)


def get_identifiers_from_items(
    item: Dict[str, Any],
    fallback_item: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[int]]:
    if not isinstance(item, dict):
        raise TypeError(f"Expected dict for item, got {type(item)}: {item}")

    fallback = fallback_item if isinstance(fallback_item, dict) else {}
    imdb_raw = item.get("imdb") if item.get("imdb") is not None else fallback.get("imdb")
    kinopoisk_raw = item.get("kinopoisk") if item.get("kinopoisk") is not None else fallback.get("kinopoisk")

    imdb_id = normalize_imdb_id(imdb_raw) if imdb_raw is not None else None
    kinopoisk_id = normalize_kinopoisk_id(kinopoisk_raw)

    title = item.get("title") or fallback.get("title")
    if not imdb_id and kinopoisk_id is not None:
        log_identifier_warning_once(
            kind="missing_imdb_with_kp",
            kinopoisk_id=kinopoisk_id,
            title=title,
            message=f"Item missing/invalid IMDb ID but has Kinopoisk ID: {kinopoisk_id}, title: {title}",
        )
    elif not imdb_id and kinopoisk_id is None:
        log_identifier_warning_once(
            kind="missing_both_ids",
            kinopoisk_id=None,
            title=title,
            message=f"Item missing both IMDb and Kinopoisk IDs, title: {title}",
        )

    return imdb_id, kinopoisk_id


def split_base_url_and_prefixes(base_url: str, prefixes: List[str]) -> Tuple[str, List[str]]:
    normalized_base = base_url.rstrip("/")
    normalized_prefixes = [p if p.startswith("/") else f"/{p}" for p in prefixes]
    normalized_prefixes = [p for p in normalized_prefixes if p]

    match = re.search(r"/(v\d+)$", normalized_base)
    if not match:
        return normalized_base, normalized_prefixes

    extracted_prefix = f"/{match.group(1)}"
    clean_base = normalized_base[: -len(extracted_prefix)]
    merged_prefixes = [extracted_prefix] + [p for p in normalized_prefixes if p != extracted_prefix]
    return clean_base, merged_prefixes


def should_include_by_timestamp(item_timestamp: Any, since_dt: Optional[datetime], full_export: bool) -> bool:
    if full_export or since_dt is None:
        return True
    item_dt = ts_to_dt_utc(item_timestamp)
    if item_dt is None:
        return False
    return item_dt >= since_dt


def warn_unsupported(item_type: Any, context: str) -> None:
    logging.warning("Skipping unsupported type '%s' in %s.", item_type, context)


class KinoPubClient:
    def __init__(
        self,
        token: str,
        base_url: str,
        prefixes: Optional[List[str]] = None,
        raw_dump_enabled: bool = False,
        raw_dump_dir: Optional[Path] = None,
    ) -> None:
        base_prefixes = prefixes or ["/v1", "/v2"]
        self.base_url, self.prefixes = split_base_url_and_prefixes(base_url, base_prefixes)
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        self.raw_dump_enabled = raw_dump_enabled
        self.raw_dump_dir = Path(raw_dump_dir) if raw_dump_dir is not None else RAW_DUMP_OUTPUT_DIR
        self.raw_dump_count = 0
        self.session = requests_cache.CachedSession(
            cache_name=str(CACHE_DB_FILE),
            backend="sqlite",
            #expire_after=timedelta(hours=CACHE_TTL_HOURS),
            always_revalidate=True,
            allowable_methods=("GET",),
        )
        self.preferred_prefix = self.detect_api_prefix()

    def detect_api_prefix(self) -> str:
        """Detect the first API prefix that appears to be available."""
        for prefix in self.prefixes:
            probe_endpoint = f"{prefix}/history"
            response = self._request_raw(probe_endpoint, params={"page": 1, "perpage": 1})
            if isinstance(response, dict):
                logging.info("Detected API prefix: %s", prefix)
                return prefix
        logging.warning("Could not detect API version, defaulting to %s", self.prefixes[0])
        return self.prefixes[0]

    def request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Request endpoint using preferred prefix and fallback candidates."""
        ordered_prefixes = [self.preferred_prefix] + [p for p in self.prefixes if p != self.preferred_prefix]
        for prefix in ordered_prefixes:
            full_endpoint = f"{prefix}{endpoint}"
            response = self._request_raw(full_endpoint, params=params)
            if isinstance(response, dict):
                if prefix != self.preferred_prefix:
                    logging.info("Switching preferred API prefix from %s to %s", self.preferred_prefix, prefix)
                    self.preferred_prefix = prefix
                return response
        return None

    def _request_raw(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}{endpoint}"
        page = params.get("page") if isinstance(params, dict) else None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.get(
                    url,
                    headers=self.headers,
                    params=params,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )

                if response.status_code == 401:
                    logging.error("Authorization failed (401). Please verify KINOPUB_TOKEN.")
                    return None
                if response.status_code in {404, 410}:
                    return None
                if response.status_code >= 500:
                    raise requests.HTTPError(f"Server error {response.status_code}", response=response)

                response.raise_for_status()
                payload = response.json()
                if self.raw_dump_enabled:
                    self._write_raw_response_dump(
                        {
                            "fetched_at": ts_to_iso_utc(time.time()),
                            "url": response.url,
                            "endpoint": endpoint,
                            "params": params,
                            "status_code": response.status_code,
                            "from_cache": bool(getattr(response, "from_cache", False)),
                            "payload": payload,
                        }
                    )
                if getattr(response, "from_cache", False):
                    logging.debug("Cache hit for %s page=%s", endpoint, page)
                return payload

            except (requests.RequestException, ValueError) as exc:
                if attempt >= MAX_RETRIES:
                    logging.error(
                        "API request failed after %s attempts: %s params=%s error=%s",
                        MAX_RETRIES,
                        endpoint,
                        params,
                        exc,
                    )
                    return None
                sleep_for = BACKOFF_SECONDS * attempt
                logging.warning(
                    "Request failed (attempt %s/%s) for %s. Retrying in %.1fs.",
                    attempt,
                    MAX_RETRIES,
                    endpoint,
                    sleep_for,
                )
                time.sleep(sleep_for)
        return None

    def _write_raw_response_dump(self, dump_payload: Dict[str, Any]) -> None:
        self.raw_dump_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dump_count += 1

        fetched_at = str(dump_payload.get("fetched_at") or ts_to_iso_utc(time.time()))
        safe_ts = fetched_at.replace(":", "-").replace("+", "_")
        endpoint = str(dump_payload.get("endpoint") or "unknown")
        safe_endpoint = endpoint.strip("/").replace("/", "_") or "root"
        file_name = f"{self.raw_dump_count:05d}_{safe_ts}_{safe_endpoint}.json"
        file_path = self.raw_dump_dir / file_name

        write_output_json(file_path, dump_payload)


def fetch_paginated_items(
    client: KinoPubClient,
    endpoint: str,
    *,
    per_page: int = 50,
    items_key: str = "items",
    extra_params: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:

    logging.info("Fetching pages...")
    all_items: List[Dict[str, Any]] = []
    page = 1
    got_any_page = False
    while True:
        params = dict(extra_params or {})
        params["page"] = page
        params["perpage"] = per_page
        payload = client.request(endpoint, params=params)
        if not payload:
            if not got_any_page:
                raise RuntimeError(f"Failed to fetch {endpoint}: no successful page retrieved.")
            break
        got_any_page = True
        total_items = payload.get("pagination", {}).get("total_items")
        items = payload.get(items_key)
        if not isinstance(items, list) or not items:
            break
        all_items.extend(items)
        logging.info("Fetched page %s: %s item(s) (total_fetched_items=%s/%s)", page, len(payload.get(items_key, [])), len(all_items), total_items)
        if len(items) < per_page:
            break
        page += 1
    return all_items


def get_watchlist(
    client: KinoPubClient,
    since_dt: Optional[datetime],
    full_export: bool,
) -> Dict[str, List[Dict[str, Any]]]:
    logging.info("Exporting watchlist/favorites from bookmarks...")
    bookmarks = client.request("/bookmarks")
    if not bookmarks:
        raise RuntimeError("Failed to fetch watchlist folders from /bookmarks.")
    if "items" not in bookmarks:
        raise RuntimeError("Invalid /bookmarks response: missing 'items'.")

    watchlist = {"movies": [], "shows": []}
    watchlist_env = os.environ.get("WATCHLIST_FOLDERS", "")
    accepted_folders = {
        value.strip().lower()
        for value in watchlist_env.split(",")
        if value.strip()
    }

    for folder in bookmarks.get("items", []):
        folder_title = str(folder.get("title", "")).strip().lower()
        if accepted_folders and folder_title not in accepted_folders:
            continue

        folder_id = folder.get("id")
        if not folder_id:
            continue

        details = client.request(f"/bookmarks/{folder_id}")
        if not details:
            raise RuntimeError(f"Failed to fetch watchlist folder details for folder id {folder_id}.")

        folder_item = details.get("item", {})
        elements = folder_item.get("elements") or details.get("items") or []
        if not isinstance(elements, list):
            raise RuntimeError(f"Invalid watchlist folder payload for folder id {folder_id}: expected list.")

        for item in elements:
            item_type = item.get("type")
            if item_type not in MOVIE_TYPES and item_type not in THREE_D_TYPES and item_type not in SHOW_TYPES:
                warn_unsupported(item_type, "watchlist")
                continue

            if not should_include_by_timestamp(item.get("created"), since_dt, full_export):
                continue

            imdb_id, kinopoisk_id = get_identifiers_from_items(item)
            kinopub_id = normalize_kinopub_id(item.get("id"))
            title, original_title, year = extract_title_fields(item)
            if not imdb_id and kinopoisk_id is None:
                warn_missing_metadata_if_needed(title, original_title, year, "Watchlist")
                logging.debug("Missing metadata for watchlist item: %s", item)

            row = {
                "title": title,
                "original_title": original_title,
                "kinopub_id": kinopub_id,
                "imdb_id": imdb_id,
                "kinopoisk_id": kinopoisk_id,
                "year": year,
                "added_at": ts_to_iso_utc(item.get("created")),
            }

            if item_type in MOVIE_TYPES or item_type in THREE_D_TYPES:
                watchlist["movies"].append(row)
            else:
                watchlist["shows"].append(row)

    logging.info(
        "Watchlist exported: %s movie(s), %s show(s).",
        len(watchlist["movies"]),
        len(watchlist["shows"]),
    )
    return watchlist


def get_watching(
    client: KinoPubClient,
    history_items: List[Dict[str, Any]],
    since_dt: Optional[datetime],
    full_export: bool,
) -> Dict[str, List[Dict[str, Any]]]:
    logging.info("Exporting watching shows from history payload...")
    latest_by_show: Dict[Tuple[str, Any], Dict[str, Any]] = {}
    finished_by_kinopub_id: Dict[int, Optional[bool]] = {}

    def extract_finished_from_watching_details(kinopub_id: int) -> Optional[bool]:
        if kinopub_id in finished_by_kinopub_id:
            return finished_by_kinopub_id[kinopub_id]

        details = client.request("/watching", params={"id": kinopub_id})
        detail_item = details.get("item") if isinstance(details, dict) else None
        seasons = detail_item.get("seasons") if isinstance(detail_item, dict) else None
        if not isinstance(seasons, list) or not seasons:
            finished_by_kinopub_id[kinopub_id] = None
            return None

        season_statuses: List[int] = []
        for season_row in seasons:
            if not isinstance(season_row, dict):
                continue
            season_number = int(season_row.get("number") or 0)
            if season_number <= 0:
                continue
            try:
                status = int(season_row.get("status"))
            except (TypeError, ValueError):
                status = -1
            season_statuses.append(status)

        if not season_statuses:
            finished_by_kinopub_id[kinopub_id] = None
            return None

        is_finished = all(status == 1 for status in season_statuses)
        finished_by_kinopub_id[kinopub_id] = is_finished
        return is_finished

    for history_item in history_items:
        if not isinstance(history_item, dict):
            continue

        watched_ts = history_item.get("last_seen") or history_item.get("first_seen")
        if not should_include_by_timestamp(watched_ts, since_dt, full_export):
            continue

        raw_item_meta = history_item.get("item")
        raw_media_meta = history_item.get("media")
        item_meta: Dict[str, Any] = raw_item_meta if isinstance(raw_item_meta, dict) else {}
        media_meta: Dict[str, Any] = raw_media_meta if isinstance(raw_media_meta, dict) else {}

        item_type = item_meta.get("type")
        if item_type not in SHOW_TYPES:
            continue

        season = int(media_meta.get("snumber") or media_meta.get("season") or 1)
        episode = int(media_meta.get("number") or media_meta.get("episode") or 1)
        if season == 0:
            continue

        imdb_id, kinopoisk_id = get_identifiers_from_items(item_meta)
        kinopub_id = normalize_kinopub_id(item_meta.get("id"))
        if kinopub_id is None:
            kinopub_id = normalize_kinopub_id(history_item.get("id"))

        title, original_title, year = extract_title_fields(item_meta)
        if not imdb_id and kinopoisk_id is None:
            warn_missing_metadata_if_needed(title, original_title, year, "Watching")

        watched_dt = ts_to_dt_utc(watched_ts) or utc_now()
        last_viewed_at = watched_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        is_subscribed_raw = item_meta.get("subscribed")
        is_subscribed = bool(is_subscribed_raw) if is_subscribed_raw is not None else True
        fallback_finished = bool(item_meta.get("finished"))

        finished_from_watching: Optional[bool] = None
        if kinopub_id is not None:
            finished_from_watching = extract_finished_from_watching_details(kinopub_id)
        is_finished = finished_from_watching if finished_from_watching is not None else fallback_finished

        if kinopub_id is not None:
            show_key: Tuple[str, Any] = ("kinopub_id", kinopub_id)
        elif imdb_id:
            show_key = ("imdb_id", imdb_id)
        elif kinopoisk_id is not None:
            show_key = ("kinopoisk_id", kinopoisk_id)
        elif title:
            show_key = ("title_year", f"{title.lower()}::{year}")
        else:
            continue

        sort_key = (watched_dt.timestamp(), season, episode)
        progress_key = (season, episode)
        existing = latest_by_show.get(show_key)
        if existing is None:
            latest_by_show[show_key] = {
                "_sort_key": sort_key,
                "_max_progress_key": progress_key,
                "_is_subscribed": is_subscribed,
                "_is_finished": is_finished,
                "title": title,
                "original_title": original_title,
                "kinopub_id": kinopub_id,
                "imdb_id": imdb_id,
                "kinopoisk_id": kinopoisk_id,
                "year": year,
                "last_viewed_at": last_viewed_at,
            }
            continue

        if progress_key > existing["_max_progress_key"]:
            existing["_max_progress_key"] = progress_key

        # Keep show metadata and subscription flags from the latest viewed event.
        if sort_key >= existing["_sort_key"]:
            existing["_sort_key"] = sort_key
            existing["_is_subscribed"] = is_subscribed
            existing["_is_finished"] = is_finished
            existing["title"] = title
            existing["original_title"] = original_title
            existing["kinopub_id"] = kinopub_id
            existing["imdb_id"] = imdb_id
            existing["kinopoisk_id"] = kinopoisk_id
            existing["year"] = year
            existing["last_viewed_at"] = last_viewed_at

    watching: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    sorted_rows = sorted(
        latest_by_show.values(),
        key=lambda row: row["_sort_key"],
        reverse=True,
    )

    for row in sorted_rows:
        max_season, max_episode = row["_max_progress_key"]
        payload_row = {
            "title": row["title"],
            "original_title": row["original_title"],
            "kinopub_id": row["kinopub_id"],
            "imdb_id": row["imdb_id"],
            "kinopoisk_id": row["kinopoisk_id"],
            "year": row["year"],
            "progress": {
                "last_watched_season": max_season,
                "last_watched_episode": max_episode,
                "is_finished": row["_is_finished"],
            },
            "last_viewed_at": row["last_viewed_at"],
        }
        if row["_is_subscribed"]:
            watching.append(payload_row)
        else:
            dropped.append(payload_row)

    logging.info("Watching exported: %s active show(s), %s dropped show(s).", len(watching), len(dropped))
    return {"watching": watching, "dropped": dropped}


def get_history(
    history_items: List[Dict[str, Any]],
    since_dt: Optional[datetime],
    full_export: bool,
) -> List[Dict[str, Any]]:
    logging.info("Exporting watch history from fetched history payload...")
    logging.info("Fetched raw history records: %s", len(history_items))

    result: List[Dict[str, Any]] = []
    seen_movies: Dict[Tuple[str, bool], List[datetime]] = {}

    for item in history_items:
        watched_ts = item.get("last_seen") or item.get("first_seen")
        if not should_include_by_timestamp(watched_ts, since_dt, full_export):
            continue

        raw_item_meta = item.get("item")
        raw_media_meta = item.get("media")
        item_meta: Dict[str, Any] = raw_item_meta if isinstance(raw_item_meta, dict) else {}
        media_meta: Dict[str, Any] = raw_media_meta if isinstance(raw_media_meta, dict) else {}

        imdb_id, kinopoisk_id = get_identifiers_from_items(item_meta)
        kinopub_id = normalize_kinopub_id(item_meta.get("id"))
        if kinopub_id is None:
            kinopub_id = normalize_kinopub_id(item.get("id"))

        title, original_title, year = extract_title_fields(item_meta)
        if not imdb_id and kinopoisk_id is None:
            warn_missing_metadata_if_needed(title, original_title, year, "History")

        item_type = item_meta.get("type")
        watched_at_dt = ts_to_dt_utc(watched_ts) or utc_now()
        watched_at = watched_at_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

        if item_type in SHOW_TYPES:
            season = int(media_meta.get("snumber") or media_meta.get("season") or 1)
            episode = int(media_meta.get("number") or media_meta.get("episode") or 1)
            if season == 0:
                continue
            result.append(
                {
                    "title": title,
                    "original_title": original_title,
                    "kinopub_id": kinopub_id,
                    "imdb_id": imdb_id,
                    "kinopoisk_id": kinopoisk_id,
                    "year": year,
                    "type": "show",
                    "season": season,
                    "episode": episode,
                    "watched_at": watched_at,
                }
            )
            continue

        if item_type in MOVIE_TYPES or item_type in THREE_D_TYPES:
            video_title = str(media_meta.get("title", "")).lower()
            if any(marker in video_title for marker in ["trailer", "making of", "трейлер", "доп. материалы"]):
                logging.debug("Skipping history item with trailer/extra content: %s", video_title)
                continue

            is_3d = item_type in THREE_D_TYPES
            dedup_identifier = imdb_id or (f"kp:{kinopoisk_id}" if kinopoisk_id is not None else f"title:{title}|year:{year}")
            timestamps = seen_movies.setdefault((dedup_identifier, is_3d), [])
            if any(abs(watched_at_dt - previous) < timedelta(hours=24) for previous in timestamps):
                logging.debug("Skipping duplicate history item within 24 hours: %s", title)
                continue
            timestamps.append(watched_at_dt)

            result.append(
                {
                    "title": title,
                    "original_title": original_title,
                    "kinopub_id": kinopub_id,
                    "imdb_id": imdb_id,
                    "kinopoisk_id": kinopoisk_id,
                    "year": year,
                    "type": "movie",
                    "is_3d": is_3d,
                    "season": None,
                    "episode": None,
                    "watched_at": watched_at,
                }
            )
            continue

        warn_unsupported(item_type, "history")

    logging.info("History exported after filtering: %s record(s) from %s total.", len(result), len(history_items))
    return result


def write_output_json(file_path: Path, payload: dict[str, Any]) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("w", encoding="utf-8") as file_handle:
        json.dump(payload, file_handle, ensure_ascii=False, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export KinoPub watch data to JSON files.")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run full export. Default mode is incremental sync.",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Override incremental start timestamp (ISO-8601, e.g. 2026-07-05T12:00:00Z).",
    )
    parser.add_argument(
        "--history-file",
        type=Path,
        default=HISTORY_OUTPUT_FILE,
        help="History JSON file used to detect last incremental timestamp.",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=BASE_URL,
        help="KinoPub API base URL.",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=TOKEN,
        help="Bearer token for KinoPub API (or use KINOPUB_TOKEN env var).",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (or use KINOPUB_LOG_LEVEL env var).",
    )
    parser.add_argument(
        "--raw-dump",
        nargs="?",
        const=RAW_DUMP_OUTPUT_DIR,
        default=None,
        type=Path,
        help="Write each successful API JSON response into a separate file in a dump directory. Optionally pass a custom directory path.",
    )
    return parser.parse_args()


def setup_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def main() -> int:
    args = parse_args()
    setup_logging(args.log_level)

    if not args.token or args.token == TOKEN_PLACEHOLDER:
        logging.error("A valid token is required. Set KINOPUB_TOKEN or pass --token.")
        return 1

    history_since = extract_last_history_timestamp(args.history_file)
    cli_since = parse_iso_utc(args.since)

    if args.since and cli_since is None:
        logging.error("Invalid --since value. Use ISO-8601, e.g. 2026-07-05T12:00:00Z")
        return 1

    if args.full:
        since_dt = None
        logging.info("Sync mode: full export.")
    else:
        since_dt = cli_since or history_since
        if since_dt:
            logging.info("Sync mode: incremental since %s", since_dt.isoformat().replace("+00:00", "Z"))
        else:
            logging.info("Sync mode: incremental without history timestamp (acts like first full run).")

    prefixes_env = os.environ.get("KINOPUB_API_PREFIXES", "v1,v2")
    prefixes = [f"/{p.strip().lstrip('/')}" for p in prefixes_env.split(",") if p.strip()]
    if not prefixes:
        prefixes = ["/v1", "/v2"]

    client = KinoPubClient(
        token=args.token,
        base_url=args.base_url,
        prefixes=prefixes,
        raw_dump_enabled=bool(args.raw_dump),
        raw_dump_dir=args.raw_dump,
    )
    logging.info("HTTP cache enabled: %s (TTL=%sh fallback)", CACHE_DB_FILE, CACHE_TTL_HOURS)

    try:
        watchlist = get_watchlist(client, since_dt=since_dt, full_export=args.full)
        history_items = fetch_paginated_items(client, "/history", per_page=50, items_key="history")
        history = get_history(history_items, since_dt=since_dt, full_export=args.full)
        watching = get_watching(
            client,
            history_items,
            since_dt=since_dt,
            full_export=args.full,
        )
    except RuntimeError as exc:
        logging.error("Export aborted: %s", exc)
        return 1

    write_output_json(WATCHLIST_OUTPUT_FILE, {"watchlist": watchlist})
    write_output_json(WATCHING_OUTPUT_FILE, watching)
    write_output_json(HISTORY_OUTPUT_FILE, {"history": history})
    if args.raw_dump:
        logging.info("Raw API dumps saved: %s file(s) in %s", client.raw_dump_count, client.raw_dump_dir)

    logging.info("Export completed successfully.")
    logging.info(
        "Generated files: %s, %s, %s",
        WATCHLIST_OUTPUT_FILE,
        WATCHING_OUTPUT_FILE,
        HISTORY_OUTPUT_FILE,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())