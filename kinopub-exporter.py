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
CURRENTLY_WATCHING_OUTPUT_FILE = OUTPUT_DIR / "currently_watching.json"
HISTORY_OUTPUT_FILE = OUTPUT_DIR / "history.json"
REQUEST_TIMEOUT_SECONDS = 25
MAX_RETRIES = 3
BACKOFF_SECONDS = 1.0
LOG_LEVEL = os.environ.get("KINOPUB_LOG_LEVEL", "INFO")

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
    def __init__(self, token: str, base_url: str, prefixes: Optional[List[str]] = None) -> None:
        base_prefixes = prefixes or ["/v1", "/v2"]
        self.base_url, self.prefixes = split_base_url_and_prefixes(base_url, base_prefixes)
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        self.session = requests_cache.CachedSession(
            cache_name=str(CACHE_DB_FILE),
            backend="sqlite",
            expire_after=timedelta(hours=CACHE_TTL_HOURS),
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
            title, original_title, year = extract_title_fields(item)
            if not imdb_id and kinopoisk_id is None:
                warn_missing_metadata_if_needed(title, original_title, year, "Watchlist")
                logging.debug("Missing metadata for watchlist item: %s", item)

            row = {
                "title": title,
                "original_title": original_title,
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


def extract_last_progress(detail_item: Dict[str, Any]) -> Tuple[int, int, Optional[float]]:
    seasons = detail_item.get("seasons")
    if not isinstance(seasons, list):
        return 1, 1, None

    best_key: Tuple[float, int, int] = (0.0, 1, 1)
    found = False

    for season in seasons:
        if not isinstance(season, dict):
            continue
        season_number = int(season.get("number") or 0)
        if season_number <= 0:
            continue

        episodes = season.get("episodes")
        if not isinstance(episodes, list):
            continue

        for episode in episodes:
            if not isinstance(episode, dict):
                continue

            episode_number = int(episode.get("number") or 0)
            if episode_number <= 0:
                continue

            status = int(episode.get("status") or -1)
            watched_time = float(episode.get("time") or 0)
            if status < 0 and watched_time <= 0:
                continue

            updated = float(episode.get("updated") or 0)
            key = (updated, season_number, episode_number)
            if key >= best_key:
                best_key = key
                found = True

    if not found:
        return 1, 1, None

    return best_key[1], best_key[2], best_key[0] if best_key[0] > 0 else None


def get_currently_watching(
    client: KinoPubClient,
    since_dt: Optional[datetime],
    full_export: bool,
) -> List[Dict[str, Any]]:
    logging.info("Exporting currently watching shows...")
    items = fetch_paginated_items(client, "/watching/serials", per_page=50)
    currently_watching: List[Dict[str, Any]] = []

    for item in items:
        item_type = item.get("type")
        if item_type not in SHOW_TYPES:
            if item_type not in MOVIE_TYPES and item_type not in THREE_D_TYPES:
                warn_unsupported(item_type, "currently_watching")
            continue

        details = client.request("/watching", params={"id": item.get("id")}) if item.get("id") else None
        detail_item = details.get("item", {}) if isinstance(details, dict) else {}

        updated_ts = item.get("updated") or item.get("updated_at") or detail_item.get("updated") or detail_item.get("updated_at")
        if not should_include_by_timestamp(updated_ts, since_dt, full_export):
            continue

        imdb_id, kinopoisk_id = get_identifiers_from_items(detail_item, item)
        title, original_title, year = extract_title_fields(item, detail_item)
        if not imdb_id and kinopoisk_id is None:
            warn_missing_metadata_if_needed(title, original_title, year, "Currently watching")

        last_season, last_episode, detail_updated_ts = extract_last_progress(detail_item)
        if last_season == 1 and last_episode == 1:
            last_season = int(item.get("season") or detail_item.get("season") or 1)
            last_episode = int(item.get("episode") or detail_item.get("episode") or 1)
        if last_season == 0:
            continue

        if detail_updated_ts:
            updated_ts = updated_ts or detail_updated_ts

        currently_watching.append(
            {
                "title": title,
                "original_title": original_title,
                "imdb_id": imdb_id,
                "kinopoisk_id": kinopoisk_id,
                "year": year,
                "progress": {
                    "last_watched_season": last_season,
                    "last_watched_episode": last_episode,
                    "is_finished": int(item.get("new") or 0) == 0 and int(item.get("total") or 0) > 0,
                },
                "last_viewed_at": ts_to_iso_utc(updated_ts),
            }
        )

    logging.info("Currently watching exported: %s show(s).", len(currently_watching))
    return currently_watching


def get_history(
    client: KinoPubClient,
    since_dt: Optional[datetime],
    full_export: bool,
) -> List[Dict[str, Any]]:
    logging.info("Exporting watch history with pagination...")
    history_items = fetch_paginated_items(client, "/history", per_page=50, items_key="history")
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
                continue

            is_3d = item_type in THREE_D_TYPES
            dedup_identifier = imdb_id or (f"kp:{kinopoisk_id}" if kinopoisk_id is not None else f"title:{title}|year:{year}")
            timestamps = seen_movies.setdefault((dedup_identifier, is_3d), [])
            if any(abs(watched_at_dt - previous) < timedelta(hours=24) for previous in timestamps):
                continue
            timestamps.append(watched_at_dt)

            result.append(
                {
                    "title": title,
                    "original_title": original_title,
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

    client = KinoPubClient(token=args.token, base_url=args.base_url, prefixes=prefixes)
    logging.info("HTTP cache enabled: %s (TTL=%sh fallback)", CACHE_DB_FILE, CACHE_TTL_HOURS)

    try:
        watchlist = get_watchlist(client, since_dt=since_dt, full_export=args.full)
        currently_watching = get_currently_watching(client, since_dt=since_dt, full_export=args.full)
        history = get_history(client, since_dt=since_dt, full_export=args.full)
    except RuntimeError as exc:
        logging.error("Export aborted: %s", exc)
        return 1

    write_output_json(WATCHLIST_OUTPUT_FILE, {"watchlist": watchlist})
    write_output_json(CURRENTLY_WATCHING_OUTPUT_FILE, {"currently_watching": currently_watching})
    write_output_json(HISTORY_OUTPUT_FILE, {"history": history})

    logging.info("Export completed successfully.")
    logging.info(
        "Generated files: %s, %s, %s",
        WATCHLIST_OUTPUT_FILE,
        CURRENTLY_WATCHING_OUTPUT_FILE,
        HISTORY_OUTPUT_FILE,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())