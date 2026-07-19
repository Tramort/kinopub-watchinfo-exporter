#!/usr/bin/env python3
"""Sync Trakt in-progress shows to a custom list for Sonarr import.

Sonarr's built-in Trakt "User Watched / In Progress" filter breaks after Trakt's
2026 watched API change: season progress is omitted unless extended=progress is
requested. This script builds that filter correctly and mirrors the result into
a Trakt list Sonarr can import (Trakt List).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests

DEFAULT_TOKEN_CACHE_FILE = Path("data/trakt_token_cache.json")
DEFAULT_STATE_FILE = Path("data/trakt_nextup_state.json")
DEFAULT_TRAKT_BASE_URL = "https://api.trakt.tv"
DEFAULT_LIST_NAME = "Sonarr Next Up"
DEFAULT_LIST_SLUG = "sonarr-next-up"
DEFAULT_PAGE_SIZE = 100
DEFAULT_BATCH_SIZE = 100
DEFAULT_RATE_LIMIT_DELAY = 1.1
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 1.0
DEFAULT_DEVICE_AUTH_TIMEOUT = 900


def dt_to_iso_z(value: datetime) -> str:
    return value.replace(microsecond=0).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def chunked(items: List[Any], size: int) -> Iterable[List[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def setup_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def load_token_cache(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def save_token_cache(path: Path, *, access_token: str, refresh_token: str, expires_at: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": dt_to_iso_z(datetime.now(tz=timezone.utc)),
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def try_refresh_trakt_token(
    *,
    client_id: str,
    client_secret: str,
    trakt_base_url: str,
    refresh_token: str,
) -> Optional[Tuple[str, str, int]]:
    token_url = f"{trakt_base_url.rstrip('/')}/oauth/token"
    try:
        response = requests.post(
            token_url,
            json={
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        logging.warning("Token refresh request failed: %s", exc)
        return None

    if response.status_code != 200:
        logging.warning("Token refresh failed with status %s", response.status_code)
        return None

    try:
        payload = response.json()
    except ValueError:
        logging.warning("Token refresh failed: invalid JSON response")
        return None

    access_token = payload.get("access_token")
    new_refresh_token = payload.get("refresh_token")
    created_at = payload.get("created_at")
    expires_in = payload.get("expires_in")

    if not isinstance(access_token, str) or not isinstance(new_refresh_token, str):
        logging.warning("Token refresh failed: access_token or refresh_token missing")
        return None

    if isinstance(created_at, int) and isinstance(expires_in, int):
        expires_at = created_at + expires_in
    else:
        expires_at = int(time.time()) + 3600

    return access_token, new_refresh_token, expires_at


def get_oauth_token_via_device_flow(
    *,
    client_id: str,
    client_secret: str,
    trakt_base_url: str,
    timeout_seconds: int,
) -> Tuple[str, str, int]:
    base_url = trakt_base_url.rstrip("/")
    device_code_url = f"{base_url}/oauth/device/code"
    device_token_url = f"{base_url}/oauth/device/token"

    try:
        code_response = requests.post(device_code_url, json={"client_id": client_id}, timeout=20)
        code_response.raise_for_status()
        code_payload = code_response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Failed to start Trakt device auth: {exc}") from exc

    device_code = code_payload.get("device_code")
    user_code = code_payload.get("user_code")
    verification_url = code_payload.get("verification_url")
    expires_in = code_payload.get("expires_in")
    interval = code_payload.get("interval")

    if not isinstance(device_code, str) or not isinstance(user_code, str) or not isinstance(verification_url, str):
        raise RuntimeError("Invalid Trakt device auth response: missing required fields")

    poll_interval = int(interval) if isinstance(interval, int) and interval > 0 else 5
    auth_ttl = int(expires_in) if isinstance(expires_in, int) and expires_in > 0 else timeout_seconds
    deadline = time.time() + min(timeout_seconds, auth_ttl)

    logging.info("Trakt device auth required. Open %s and enter code: %s", verification_url, user_code)
    logging.info("Waiting for authorization... (poll interval: %ss)", poll_interval)

    while time.time() < deadline:
        try:
            token_response = requests.post(
                device_token_url,
                json={
                    "code": device_code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=20,
            )
        except requests.RequestException as exc:
            logging.warning("Device auth polling error: %s", exc)
            time.sleep(poll_interval)
            continue

        if token_response.status_code == 200:
            token_payload = token_response.json()
            access_token = token_payload.get("access_token")
            refresh_token = token_payload.get("refresh_token")
            created_at = token_payload.get("created_at")
            token_expires_in = token_payload.get("expires_in")
            if not isinstance(access_token, str) or not isinstance(refresh_token, str):
                raise RuntimeError("Invalid token response: access_token or refresh_token is missing")
            if isinstance(created_at, int) and isinstance(token_expires_in, int):
                expires_at = created_at + token_expires_in
            else:
                expires_at = int(time.time()) + 3600
            logging.info("Device auth completed successfully.")
            return access_token, refresh_token, expires_at

        if token_response.status_code == 400:
            time.sleep(poll_interval)
            continue
        if token_response.status_code == 429:
            poll_interval += 2
            logging.warning("Device auth polling too fast, increasing interval to %ss", poll_interval)
            time.sleep(poll_interval)
            continue
        if token_response.status_code == 410:
            raise RuntimeError("Device code expired before authorization completed")
        if token_response.status_code == 418:
            raise RuntimeError("Device authorization denied by user")
        if token_response.status_code == 404:
            raise RuntimeError("Invalid device code while polling token endpoint")
        if token_response.status_code == 409:
            raise RuntimeError("Device code already used")

        raise RuntimeError(f"Unexpected device auth response: {token_response.status_code} {token_response.text}")

    raise RuntimeError("Device auth timed out before authorization completed")


def get_cached_or_device_tokens(
    *,
    client_id: str,
    client_secret: str,
    trakt_base_url: str,
    timeout_seconds: int,
    token_cache_file: Path,
) -> Tuple[str, str, int]:
    cached = load_token_cache(token_cache_file)
    now = int(time.time())

    if isinstance(cached, dict):
        cached_access = cached.get("access_token")
        cached_refresh = cached.get("refresh_token")
        cached_expires = cached.get("expires_at")

        if isinstance(cached_access, str) and isinstance(cached_refresh, str) and isinstance(cached_expires, int):
            if cached_expires > now + 60:
                logging.info("Using cached Trakt access token from %s", token_cache_file)
                return cached_access, cached_refresh, cached_expires

            refreshed = try_refresh_trakt_token(
                client_id=client_id,
                client_secret=client_secret,
                trakt_base_url=trakt_base_url,
                refresh_token=cached_refresh,
            )
            if refreshed:
                access_token, refresh_token, expires_at = refreshed
                save_token_cache(
                    token_cache_file,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    expires_at=expires_at,
                )
                logging.info("Refreshed Trakt access token and updated cache %s", token_cache_file)
                return access_token, refresh_token, expires_at

            logging.warning("Cached Trakt token could not be refreshed; falling back to device auth")

    access_token, refresh_token, expires_at = get_oauth_token_via_device_flow(
        client_id=client_id,
        client_secret=client_secret,
        trakt_base_url=trakt_base_url,
        timeout_seconds=timeout_seconds,
    )
    save_token_cache(
        token_cache_file,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )
    logging.info("Stored Trakt token cache at %s", token_cache_file)
    return access_token, refresh_token, expires_at


class TraktApi:
    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        access_token: str,
        rate_limit_delay: float,
        retry_attempts: int,
        backoff_seconds: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.rate_limit_delay = rate_limit_delay
        self.retry_attempts = retry_attempts
        self.backoff_seconds = backoff_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "trakt-api-version": "2",
                "trakt-api-key": client_id,
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "kinopub-watchinfo-exporter/trakt-nextup",
            }
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_error: Optional[Exception] = None

        for attempt in range(1, self.retry_attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    timeout=120,
                )
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    sleep_for = float(retry_after) if retry_after and retry_after.isdigit() else self.rate_limit_delay * 2
                    logging.warning(
                        "Rate limited on %s %s (attempt %s/%s). Sleeping %.1fs.",
                        method,
                        path,
                        attempt,
                        self.retry_attempts,
                        sleep_for,
                    )
                    time.sleep(sleep_for)
                    continue
                if 500 <= response.status_code < 600:
                    sleep_for = self.backoff_seconds * attempt
                    logging.warning(
                        "Trakt temporary error %s on %s %s (attempt %s/%s). Sleeping %.1fs.",
                        response.status_code,
                        method,
                        path,
                        attempt,
                        self.retry_attempts,
                        sleep_for,
                    )
                    time.sleep(sleep_for)
                    continue
                time.sleep(self.rate_limit_delay)
                return response
            except requests.RequestException as exc:
                last_error = exc
                sleep_for = self.backoff_seconds * attempt
                logging.warning(
                    "Network error on %s %s (attempt %s/%s): %s. Sleeping %.1fs.",
                    method,
                    path,
                    attempt,
                    self.retry_attempts,
                    exc,
                    sleep_for,
                )
                time.sleep(sleep_for)

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Failed request {method} {path}")


def count_watched_regular_episodes(seasons: Any) -> Optional[int]:
    if seasons is None:
        return None
    if not isinstance(seasons, list):
        return None
    total = 0
    for season in seasons:
        if not isinstance(season, dict):
            return None
        number = season.get("number")
        episodes = season.get("episodes")
        if not isinstance(number, int):
            return None
        if number <= 0:
            continue
        if episodes is None:
            return None
        if not isinstance(episodes, list):
            return None
        total += len(episodes)
    return total


def is_in_progress_show(item: Dict[str, Any]) -> bool:
    """Match Sonarr In Progress logic when season progress is present."""
    show = item.get("show")
    if not isinstance(show, dict):
        return False
    aired = show.get("aired_episodes")
    if not isinstance(aired, int) or aired <= 0:
        return False
    watched = count_watched_regular_episodes(item.get("seasons"))
    if watched is None:
        return False
    return watched < aired


def extract_show_ids(show: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ids = show.get("ids")
    if not isinstance(ids, dict):
        return None
    trakt_id = ids.get("trakt")
    imdb_id = ids.get("imdb")
    payload: Dict[str, Any] = {}
    if isinstance(trakt_id, int):
        payload["trakt"] = trakt_id
    if isinstance(imdb_id, str) and imdb_id.strip():
        payload["imdb"] = imdb_id.strip()
    return payload or None


def show_key_from_ids(ids: Dict[str, Any]) -> Optional[str]:
    trakt_id = ids.get("trakt")
    if isinstance(trakt_id, int):
        return f"trakt:{trakt_id}"
    imdb_id = ids.get("imdb")
    if isinstance(imdb_id, str) and imdb_id.strip():
        return f"imdb:{imdb_id.strip()}"
    return None


def paginate_get(
    api: TraktApi,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    page_size: int,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    page = 1
    query = dict(params or {})
    while True:
        query.update({"page": page, "limit": page_size})
        response = api.request("GET", path, params=query)
        if response.status_code != 200:
            raise RuntimeError(f"Failed to fetch {path}: HTTP {response.status_code} {response.text[:300]}")
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError(f"Unexpected payload for {path}: expected a list")
        if not payload:
            break
        items.extend(item for item in payload if isinstance(item, dict))
        page_count_header = response.headers.get("X-Pagination-Page-Count")
        if page_count_header and page_count_header.isdigit():
            if page >= int(page_count_header):
                break
        elif len(payload) < page_size:
            break
        page += 1
    return items


def fetch_watched_shows(api: TraktApi, *, page_size: int) -> List[Dict[str, Any]]:
    return paginate_get(
        api,
        "/users/me/watched/shows",
        params={"extended": "progress"},
        page_size=page_size,
    )


def fetch_dropped_show_keys(api: TraktApi, *, page_size: int = 100) -> Set[str]:
    """Return show keys hidden as dropped on Trakt (users/hidden/dropped)."""
    keys: Set[str] = set()
    items = paginate_get(
        api,
        "/users/hidden/dropped",
        params={"type": "show"},
        page_size=page_size,
    )
    for entry in items:
        show = entry.get("show")
        if not isinstance(show, dict):
            continue
        ids = extract_show_ids(show)
        if not ids:
            continue
        key = show_key_from_ids(ids)
        if key:
            keys.add(key)
    return keys


def fetch_user_lists(api: TraktApi) -> List[Dict[str, Any]]:
    response = api.request("GET", "/users/me/lists")
    if response.status_code != 200:
        raise RuntimeError(f"Failed to fetch user lists: HTTP {response.status_code} {response.text[:300]}")
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected lists payload: expected a list")
    return [item for item in payload if isinstance(item, dict)]


def create_list(api: TraktApi, *, name: str, description: str) -> Dict[str, Any]:
    response = api.request(
        "POST",
        "/users/me/lists",
        json_body={
            "name": name,
            "description": description,
            "privacy": "private",
            "display_numbers": False,
            "allow_comments": False,
            "sort_by": "rank",
            "sort_how": "asc",
        },
    )
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"Failed to create list: HTTP {response.status_code} {response.text[:300]}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected create-list payload")
    return payload


def find_list(api: TraktApi, *, name: str, slug: str) -> Optional[str]:
    for entry in fetch_user_lists(api):
        ids = entry.get("ids") if isinstance(entry.get("ids"), dict) else {}
        entry_slug = ids.get("slug") if isinstance(ids, dict) else None
        entry_name = entry.get("name")
        if entry_slug == slug or entry_name == name:
            resolved_slug = entry_slug if isinstance(entry_slug, str) and entry_slug else slug
            logging.info("Using existing Trakt list: name=%s slug=%s", entry_name, resolved_slug)
            return resolved_slug
    return None


def ensure_list(api: TraktApi, *, name: str, slug: str, create: bool) -> Optional[str]:
    """Return list slug, creating the list when create=True and missing."""
    existing = find_list(api, name=name, slug=slug)
    if existing:
        return existing
    if not create:
        logging.info("List %s does not exist yet (dry-run will not create it).", slug)
        return None

    created = create_list(
        api,
        name=name,
        description=(
            "Auto-synced in-progress shows for Sonarr. "
            "Generated by trakt-nextup.py because Sonarr In Progress "
            "breaks after Trakt watched API season-progress changes."
        ),
    )
    ids = created.get("ids") if isinstance(created.get("ids"), dict) else {}
    created_slug = ids.get("slug") if isinstance(ids, dict) else None
    resolved_slug = created_slug if isinstance(created_slug, str) and created_slug else slug
    logging.info("Created Trakt list: name=%s slug=%s", name, resolved_slug)
    return resolved_slug


def fetch_list_show_items(api: TraktApi, list_id: str) -> Dict[str, Dict[str, Any]]:
    items: Dict[str, Dict[str, Any]] = {}
    page = 1
    page_size = 100
    while True:
        response = api.request(
            "GET",
            f"/users/me/lists/{list_id}/items/shows",
            params={"page": page, "limit": page_size},
        )
        if response.status_code != 200:
            raise RuntimeError(f"Failed to fetch list items: HTTP {response.status_code} {response.text[:300]}")
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("Unexpected list items payload: expected a list")
        if not payload:
            break
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            show = entry.get("show")
            if not isinstance(show, dict):
                continue
            ids = extract_show_ids(show)
            if not ids:
                continue
            key = show_key_from_ids(ids)
            if key:
                items[key] = ids
        page_count_header = response.headers.get("X-Pagination-Page-Count")
        if page_count_header and page_count_header.isdigit():
            if page >= int(page_count_header):
                break
        elif len(payload) < page_size:
            break
        page += 1
    return items


def post_list_items(
    api: TraktApi,
    list_id: str,
    *,
    path_suffix: str,
    shows: List[Dict[str, Any]],
    batch_size: int,
) -> int:
    changed = 0
    for batch in chunked(shows, batch_size):
        response = api.request(
            "POST",
            f"/users/me/lists/{list_id}/items{path_suffix}",
            json_body={"shows": [{"ids": ids} for ids in batch]},
        )
        if response.status_code not in {200, 201}:
            raise RuntimeError(
                f"Failed list items {path_suffix or 'add'}: HTTP {response.status_code} {response.text[:300]}"
            )
        payload = response.json() if response.content else {}
        if isinstance(payload, dict):
            bucket = payload.get("added") if path_suffix == "" else payload.get("deleted")
            if isinstance(bucket, dict) and isinstance(bucket.get("shows"), int):
                changed += bucket["shows"]
            else:
                changed += len(batch)
        else:
            changed += len(batch)
    return changed


def write_state(
    path: Path,
    *,
    dry_run: bool,
    list_slug: str,
    watched_total: int,
    dropped_total: int,
    skipped_dropped: int,
    in_progress: List[Dict[str, Any]],
    current_count: int,
    to_add: int,
    to_remove: int,
    added: int,
    removed: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": dt_to_iso_z(datetime.now(tz=timezone.utc)),
        "dry_run": dry_run,
        "list_slug": list_slug,
        "watched_total": watched_total,
        "dropped_total": dropped_total,
        "skipped_dropped": skipped_dropped,
        "in_progress_count": len(in_progress),
        "list_before_count": current_count,
        "to_add": to_add,
        "to_remove": to_remove,
        "added": added,
        "removed": removed,
        "in_progress": in_progress,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sync Trakt in-progress shows into a custom list for Sonarr "
            "(workaround for broken User Watched / In Progress import list)."
        )
    )
    parser.add_argument("--token-cache-file", type=Path, default=DEFAULT_TOKEN_CACHE_FILE)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--trakt-client-id", type=str, default=os.environ.get("TRAKT_CLIENT_ID"))
    parser.add_argument("--trakt-client-secret", type=str, default=os.environ.get("TRAKT_CLIENT_SECRET"))
    parser.add_argument("--trakt-base-url", type=str, default=DEFAULT_TRAKT_BASE_URL)
    parser.add_argument("--list-name", type=str, default=DEFAULT_LIST_NAME)
    parser.add_argument("--list-slug", type=str, default=DEFAULT_LIST_SLUG)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--retry-attempts", type=int, default=DEFAULT_RETRY_ATTEMPTS)
    parser.add_argument("--backoff-seconds", type=float, default=DEFAULT_BACKOFF_SECONDS)
    parser.add_argument("--rate-limit-delay", type=float, default=DEFAULT_RATE_LIMIT_DELAY)
    parser.add_argument("--device-auth-timeout", type=int, default=DEFAULT_DEVICE_AUTH_TIMEOUT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--log-level",
        type=str,
        default=os.environ.get("TRAKT_NEXTUP_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(args.log_level)

    if not args.trakt_client_id:
        logging.error("Missing Trakt client ID. Set TRAKT_CLIENT_ID or pass --trakt-client-id.")
        return 1
    if not args.trakt_client_secret:
        logging.error("Missing Trakt client secret. Set TRAKT_CLIENT_SECRET or pass --trakt-client-secret.")
        return 1
    if args.page_size <= 0 or args.page_size > 100:
        logging.error("--page-size must be between 1 and 100 (Trakt progress page cap).")
        return 1
    if args.batch_size <= 0:
        logging.error("--batch-size must be greater than 0")
        return 1
    if args.device_auth_timeout <= 0:
        logging.error("--device-auth-timeout must be greater than 0")
        return 1

    try:
        access_token, _refresh_token, _expires_at = get_cached_or_device_tokens(
            client_id=args.trakt_client_id,
            client_secret=args.trakt_client_secret,
            trakt_base_url=args.trakt_base_url,
            timeout_seconds=args.device_auth_timeout,
            token_cache_file=args.token_cache_file,
        )
    except RuntimeError as exc:
        logging.error("Trakt auth failed: %s", exc)
        return 1

    api = TraktApi(
        base_url=args.trakt_base_url,
        client_id=args.trakt_client_id,
        access_token=access_token,
        rate_limit_delay=args.rate_limit_delay,
        retry_attempts=args.retry_attempts,
        backoff_seconds=args.backoff_seconds,
    )

    logging.info("Fetching watched shows with extended=progress...")
    try:
        watched = fetch_watched_shows(api, page_size=args.page_size)
        dropped_keys = fetch_dropped_show_keys(api)
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 1
    logging.info("Fetched %s watched show(s)", len(watched))
    logging.info("Fetched %s dropped show(s) to exclude", len(dropped_keys))

    in_progress_rows: List[Dict[str, Any]] = []
    desired: Dict[str, Dict[str, Any]] = {}
    skipped_no_progress = 0
    skipped_no_ids = 0
    skipped_dropped = 0

    for item in watched:
        if not is_in_progress_show(item):
            seasons = item.get("seasons")
            if seasons is None or count_watched_regular_episodes(seasons) is None:
                skipped_no_progress += 1
            continue
        show = item["show"]
        ids = extract_show_ids(show)
        if not ids:
            skipped_no_ids += 1
            continue
        key = show_key_from_ids(ids)
        if not key:
            skipped_no_ids += 1
            continue
        if key in dropped_keys:
            skipped_dropped += 1
            continue
        desired[key] = ids
        watched_count = count_watched_regular_episodes(item.get("seasons")) or 0
        aired = show.get("aired_episodes")
        in_progress_rows.append(
            {
                "title": show.get("title"),
                "year": show.get("year"),
                "imdb": ids.get("imdb"),
                "trakt": ids.get("trakt"),
                "watched_episodes": watched_count,
                "aired_episodes": aired,
            }
        )

    logging.info(
        "In-progress shows: %s (skipped_dropped=%s skipped_no_progress=%s skipped_no_ids=%s)",
        len(desired),
        skipped_dropped,
        skipped_no_progress,
        skipped_no_ids,
    )

    try:
        list_slug = ensure_list(
            api,
            name=args.list_name,
            slug=args.list_slug,
            create=not args.dry_run,
        )
        current = fetch_list_show_items(api, list_slug) if list_slug else {}
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 1

    resolved_slug = list_slug or args.list_slug
    desired_keys: Set[str] = set(desired)
    current_keys: Set[str] = set(current)
    to_add_keys = sorted(desired_keys - current_keys)
    to_remove_keys = sorted(current_keys - desired_keys)
    to_add = [desired[key] for key in to_add_keys]
    to_remove = [current[key] for key in to_remove_keys]

    logging.info(
        "List %s: current=%s desired=%s add=%s remove=%s",
        resolved_slug,
        len(current),
        len(desired),
        len(to_add),
        len(to_remove),
    )

    added = 0
    removed = 0
    if args.dry_run:
        logging.info("Dry-run enabled; not modifying Trakt list.")
        for row in in_progress_rows[:20]:
            logging.info(
                "Would keep/add: %s (%s) watched=%s aired=%s",
                row.get("title"),
                row.get("imdb") or row.get("trakt"),
                row.get("watched_episodes"),
                row.get("aired_episodes"),
            )
        if len(in_progress_rows) > 20:
            logging.info("... and %s more", len(in_progress_rows) - 20)
    else:
        assert list_slug is not None
        try:
            if to_add:
                added = post_list_items(api, list_slug, path_suffix="", shows=to_add, batch_size=args.batch_size)
            if to_remove:
                removed = post_list_items(
                    api,
                    list_slug,
                    path_suffix="/remove",
                    shows=to_remove,
                    batch_size=args.batch_size,
                )
        except RuntimeError as exc:
            logging.error("%s", exc)
            return 1
        logging.info("List sync complete: added=%s removed=%s", added, removed)

    write_state(
        args.state_file,
        dry_run=args.dry_run,
        list_slug=resolved_slug,
        watched_total=len(watched),
        dropped_total=len(dropped_keys),
        skipped_dropped=skipped_dropped,
        in_progress=in_progress_rows,
        current_count=len(current),
        to_add=len(to_add),
        to_remove=len(to_remove),
        added=added,
        removed=removed,
    )
    logging.info("Wrote sync state to %s", args.state_file)
    logging.info(
        "Sonarr setup: Import Lists → Trakt List → username=(your Trakt username), list=%s",
        resolved_slug,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
