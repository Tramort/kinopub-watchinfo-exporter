import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import trakt.core as trakt_core
from trakt import errors as trakt_errors

DEFAULT_HISTORY_FILE = Path("data/history.json")
DEFAULT_WATCHLIST_FILE = Path("data/watchlist.json")
DEFAULT_WATCHING_FILE = Path("data/watching.json")
DEFAULT_STATE_FILE = Path("data/trakt_sync_state.json")
DEFAULT_TOKEN_CACHE_FILE = Path("data/trakt_token_cache.json")
DEFAULT_MISMATCH_APPROVE_CACHE_FILE = Path("data/trakt_mismatch_approvals.json")
DEFAULT_TRAKT_BASE_URL = "https://api.trakt.tv/"
DEFAULT_TMDB_BASE_URL = "https://api.themoviedb.org/3"
DEFAULT_RATE_LIMIT_DELAY = 1.1
DEFAULT_BATCH_SIZE = 100
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 1.0
DEFAULT_DEVICE_AUTH_TIMEOUT = 900
DEFAULT_MISMATCH_MODE = "approve"
DEFAULT_MISMATCH_MAX_GAP = 1
MISMATCH_HEURISTIC_VERSION = "v1"

IMDB_PATTERN = re.compile(r"^tt\d{7,10}$")


def redact_secret(text: str, secret: Optional[str]) -> str:
    if not secret:
        return text
    if secret not in text:
        return text
    return text.replace(secret, "***")


def redact_tmdb_key_in_text(text: str, tmdb_api_key: Optional[str]) -> str:
    return redact_secret(text, tmdb_api_key)


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


def normalize_imdb_id(value: Any) -> Optional[str]:
    if isinstance(value, (int, float)):
        digits = str(int(value))
        if 5 <= len(digits) <= 10:
            return f"tt{digits.zfill(7)}"
        return None
    if isinstance(value, str):
        raw = value.strip().lower()
        if not raw:
            return None
        if IMDB_PATTERN.match(raw):
            return raw
        if raw.isdigit() and 5 <= len(raw) <= 10:
            return f"tt{raw.zfill(7)}"
    return None


def dt_to_iso_z(value: datetime) -> str:
    return value.replace(microsecond=0).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def chunked(items: List[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def extract_added_count(response: Dict[str, Any]) -> int:
    if not isinstance(response, dict):
        return 0
    added = response.get("added")
    if not isinstance(added, dict):
        return 0
    total = 0
    for value in added.values():
        if isinstance(value, int):
            total += value
    return total


def extract_deleted_count(response: Dict[str, Any]) -> int:
    if not isinstance(response, dict):
        return 0
    deleted = response.get("deleted")
    if not isinstance(deleted, dict):
        return 0
    total = 0
    for value in deleted.values():
        if isinstance(value, int):
            total += value
    return total


@dataclass
class BuildStats:
    prepared: int = 0
    skipped: int = 0
    enriched: int = 0


@dataclass
class MismatchStats:
    proposals_infer: int = 0
    proposals_drop: int = 0
    applied_infer: int = 0
    applied_drop: int = 0
    skipped_unapproved: int = 0
    skipped_rejected: int = 0
    unresolved: int = 0


@dataclass
class MismatchProposal:
    action_type: str
    fingerprint: str
    imdb_id: str
    title: str
    season: Optional[int]
    source_episode: Optional[int]
    inferred_episodes: List[int]
    watched_at: Optional[str]
    kinopub_season_count: Optional[int] = None
    trakt_season_count: Optional[int] = None


class TMDBResolver:
    def __init__(self, api_key: Optional[str], base_url: str = DEFAULT_TMDB_BASE_URL, timeout: int = 15) -> None:
        self.api_key = api_key
        if not self.api_key:
            logging.warning("TMDB API key not provided. IMDb resolution will be skipped.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def resolve_imdb_id(
        self,
        *,
        title: Optional[str],
        original_title: Optional[str],
        year: Optional[int],
        media_type: str,
    ) -> Optional[str]:
        if not self.api_key:
            return None

        candidates = []
        for candidate in [original_title, title]:
            if isinstance(candidate, str):
                trimmed = candidate.strip()
                if trimmed and trimmed not in candidates:
                    candidates.append(trimmed)

        if not candidates:
            return None

        search_kind = "movie" if media_type == "movie" else "tv"
        year_param = "year" if media_type == "movie" else "first_air_date_year"
        year_candidates: List[Optional[int]] = [None]
        if isinstance(year, int):
            year_candidates = [year, year - 1, year + 1]

        for query in candidates:
            for search_year in year_candidates:
                params: Dict[str, Any] = {"api_key": self.api_key, "query": query}
                if isinstance(search_year, int):
                    params[year_param] = search_year
                try:
                    response = self.session.get(
                        f"{self.base_url}/search/{search_kind}",
                        params=params,
                        timeout=self.timeout,
                    )
                    response.raise_for_status()
                    payload = response.json()
                except (requests.RequestException, ValueError) as exc:
                    logging.warning(
                        "TMDB search failed for %s '%s' (year=%s): %s",
                        search_kind,
                        query,
                        search_year,
                        redact_tmdb_key_in_text(str(exc), self.api_key),
                    )
                    continue

                results = payload.get("results")
                if not isinstance(results, list):
                    continue

                for item in results[:5]:
                    if not isinstance(item, dict):
                        continue
                    tmdb_id = item.get("id")
                    if not isinstance(tmdb_id, int):
                        continue
                    imdb_id = self._lookup_external_imdb(search_kind, tmdb_id)
                    normalized = normalize_imdb_id(imdb_id)
                    if normalized:
                        if isinstance(search_year, int) and search_year != year:
                            logging.info(
                                "TMDB resolved IMDb via year fallback: query=%s requested_year=%s matched_year=%s imdb=%s",
                                query,
                                year,
                                search_year,
                                normalized,
                            )
                        return normalized
        return None

    def _lookup_external_imdb(self, search_kind: str, tmdb_id: int) -> Optional[str]:
        try:
            response = self.session.get(
                f"{self.base_url}/{search_kind}/{tmdb_id}/external_ids",
                params={"api_key": self.api_key},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            return None
        value = payload.get("imdb_id") if isinstance(payload, dict) else None
        return value if isinstance(value, str) else None


class TraktClient:
    def __init__(
        self,
        *,
        client_id: str,
        access_token: str,
        client_secret: Optional[str],
        base_url: str,
        refresh_token: Optional[str] = None,
        expires_at: Optional[int] = None,
    ) -> None:
        normalized_base = base_url.rstrip("/") + "/"

        trakt_core.BASE_URL = normalized_base
        trakt_core.CLIENT_ID = client_id
        trakt_core.CLIENT_SECRET = client_secret
        trakt_core.OAUTH_TOKEN = access_token
        trakt_core.OAUTH_REFRESH = refresh_token
        trakt_core.OAUTH_EXPIRES_AT = expires_at

        trakt_core.config.cache_clear()
        trakt_core.api.cache_clear()

        self.client = trakt_core.api()

    def post_with_retry(
        self,
        endpoint: str,
        payload: Dict[str, Any],
        *,
        retries: int,
        backoff_seconds: float,
        rate_limit_delay: float,
    ) -> Dict[str, Any]:
        for attempt in range(1, retries + 1):
            try:
                response = self.client.post(endpoint, payload)
                time.sleep(rate_limit_delay)
                return response if isinstance(response, dict) else {}
            except trakt_errors.OAuthException:
                raise
            except trakt_errors.RateLimitException as exc:
                if attempt >= retries:
                    raise
                sleep_for = max(rate_limit_delay, float(exc.retry_after))
                logging.warning(
                    "Rate limited while posting %s (attempt %s/%s). Sleeping %.1fs.",
                    endpoint,
                    attempt,
                    retries,
                    sleep_for,
                )
                time.sleep(sleep_for)
            except (trakt_errors.TraktInternalException, trakt_errors.TraktBadGateway, trakt_errors.TraktUnavailable) as exc:
                if attempt >= retries:
                    raise
                sleep_for = backoff_seconds * attempt
                logging.warning(
                    "Trakt temporary error for %s (attempt %s/%s): %s. Sleeping %.1fs.",
                    endpoint,
                    attempt,
                    retries,
                    exc,
                    sleep_for,
                )
                time.sleep(sleep_for)
            except requests.RequestException as exc:
                if attempt >= retries:
                    raise
                sleep_for = backoff_seconds * attempt
                logging.warning(
                    "Network error for %s (attempt %s/%s): %s. Sleeping %.1fs.",
                    endpoint,
                    attempt,
                    retries,
                    exc,
                    sleep_for,
                )
                time.sleep(sleep_for)

        return {}

    def get_with_retry(
        self,
        endpoint: str,
        *,
        retries: int,
        backoff_seconds: float,
        rate_limit_delay: float,
    ) -> Any:
        for attempt in range(1, retries + 1):
            try:
                response = self.client.get(endpoint)
                time.sleep(rate_limit_delay)
                return response
            except trakt_errors.OAuthException:
                raise
            except trakt_errors.RateLimitException as exc:
                if attempt >= retries:
                    raise
                sleep_for = max(rate_limit_delay, float(exc.retry_after))
                logging.warning(
                    "Rate limited while requesting %s (attempt %s/%s). Sleeping %.1fs.",
                    endpoint,
                    attempt,
                    retries,
                    sleep_for,
                )
                time.sleep(sleep_for)
            except (trakt_errors.TraktInternalException, trakt_errors.TraktBadGateway, trakt_errors.TraktUnavailable) as exc:
                if attempt >= retries:
                    raise
                sleep_for = backoff_seconds * attempt
                logging.warning(
                    "Trakt temporary error for %s (attempt %s/%s): %s. Sleeping %.1fs.",
                    endpoint,
                    attempt,
                    retries,
                    exc,
                    sleep_for,
                )
                time.sleep(sleep_for)
            except (trakt_errors.TraktException, requests.RequestException) as exc:
                if attempt >= retries:
                    raise
                sleep_for = backoff_seconds * attempt
                logging.warning(
                    "Request error for %s (attempt %s/%s): %s. Sleeping %.1fs.",
                    endpoint,
                    attempt,
                    retries,
                    exc,
                    sleep_for,
                )
                time.sleep(sleep_for)
        return None

    def get_show_season_episode_counts(
        self,
        imdb_id: str,
        *,
        retries: int,
        backoff_seconds: float,
        rate_limit_delay: float,
    ) -> Optional[Dict[int, int]]:
        endpoint = f"shows/{imdb_id}/seasons?extended=episodes"
        try:
            payload = self.get_with_retry(
                endpoint,
                retries=retries,
                backoff_seconds=backoff_seconds,
                rate_limit_delay=rate_limit_delay,
            )
        except (trakt_errors.TraktException, requests.RequestException) as exc:
            logging.warning("Failed to fetch season metadata for %s: %s", imdb_id, exc)
            return None

        if not isinstance(payload, list):
            logging.warning("Unexpected season metadata payload for %s", imdb_id)
            return None

        season_counts: Dict[int, int] = {}
        for season in payload:
            if not isinstance(season, dict):
                continue
            season_number = season.get("number")
            episodes = season.get("episodes")
            if not isinstance(season_number, int) or season_number <= 0 or not isinstance(episodes, list):
                continue

            max_episode = 0
            for episode in episodes:
                if not isinstance(episode, dict):
                    continue
                episode_number = episode.get("number")
                if isinstance(episode_number, int) and episode_number > max_episode:
                    max_episode = episode_number
            if max_episode > 0:
                season_counts[season_number] = max_episode

        return season_counts


def make_infer_fingerprint(imdb_id: str, season: int, source_episode: int, inferred_episodes: List[int]) -> str:
    episodes = ",".join(str(number) for number in inferred_episodes)
    return f"infer|{MISMATCH_HEURISTIC_VERSION}|{imdb_id}|s{season}|e{source_episode}|{episodes}"


def make_drop_fingerprint(imdb_id: str) -> str:
    return f"drop|{MISMATCH_HEURISTIC_VERSION}|{imdb_id}"


def load_mismatch_approval_cache(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}

    approvals = payload.get("approvals") if isinstance(payload, dict) else None
    if not isinstance(approvals, dict):
        return {}
    return {key: value for key, value in approvals.items() if isinstance(key, str) and isinstance(value, dict)}


def save_mismatch_approval_cache(path: Path, approvals: Dict[str, Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": dt_to_iso_z(datetime.now(tz=timezone.utc)),
        "heuristic_version": MISMATCH_HEURISTIC_VERSION,
        "approvals": approvals,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def flatten_payload_items(payloads: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for payload in payloads:
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                items.append(item)
    return items


def build_payloads_from_items(items: List[Dict[str, Any]], key: str, batch_size: int) -> List[Dict[str, Any]]:
    payloads: List[Dict[str, Any]] = []
    for part in chunked(items, batch_size):
        payloads.append({key: part})
    return payloads


def dedupe_show_history_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    dedup_keys = set()
    deduped: List[Dict[str, Any]] = []
    for item in items:
        ids = item.get("ids")
        seasons = item.get("seasons")
        if not isinstance(ids, dict) or not isinstance(seasons, list) or not seasons:
            continue
        imdb_id = ids.get("imdb")
        season_payload = seasons[0] if isinstance(seasons[0], dict) else None
        if not isinstance(imdb_id, str) or not isinstance(season_payload, dict):
            continue
        season_number = season_payload.get("number")
        episodes = season_payload.get("episodes")
        if not isinstance(season_number, int) or not isinstance(episodes, list) or not episodes:
            continue
        episode_payload = episodes[0] if isinstance(episodes[0], dict) else None
        if not isinstance(episode_payload, dict):
            continue
        episode_number = episode_payload.get("number")
        watched_at = episode_payload.get("watched_at")
        if not isinstance(episode_number, int) or not isinstance(watched_at, str):
            continue
        dedup_key = (imdb_id, season_number, episode_number, watched_at)
        if dedup_key in dedup_keys:
            continue
        dedup_keys.add(dedup_key)
        deduped.append(item)
    return deduped


def dedupe_show_id_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    dedup_ids = set()
    deduped: List[Dict[str, Any]] = []
    for item in items:
        ids = item.get("ids")
        if not isinstance(ids, dict):
            continue
        imdb_id = ids.get("imdb")
        if not isinstance(imdb_id, str):
            continue
        if imdb_id in dedup_ids:
            continue
        dedup_ids.add(imdb_id)
        deduped.append(item)
    return deduped


def build_show_season_progress_from_history(
    history_items: List[Dict[str, Any]],
) -> Dict[str, Dict[int, Tuple[int, str]]]:
    progress: Dict[str, Dict[int, Tuple[int, str]]] = {}
    for row in history_items:
        if row.get("type") != "show":
            continue

        imdb_id = row.get("imdb_id")
        season = row.get("season")
        episode = row.get("episode")
        watched_at = row.get("watched_at")
        if not isinstance(imdb_id, str) or not isinstance(season, int) or not isinstance(episode, int):
            continue
        if season <= 0 or episode <= 0 or not isinstance(watched_at, str):
            continue

        season_progress = progress.setdefault(imdb_id, {})
        current = season_progress.get(season)
        if current is None or episode > current[0]:
            season_progress[season] = (episode, watched_at)

    return progress


def build_mismatch_proposals(
    watching_payload: Dict[str, List[Dict[str, Any]]],
    history_items: List[Dict[str, Any]],
    *,
    resolver: TMDBResolver,
    trakt_client: TraktClient,
    retries: int,
    backoff_seconds: float,
    rate_limit_delay: float,
    max_gap: int,
) -> Tuple[List[MismatchProposal], MismatchStats]:
    stats = MismatchStats()
    proposals: List[MismatchProposal] = []
    seen_fingerprints = set()
    season_count_cache: Dict[str, Optional[Dict[int, int]]] = {}
    history_season_progress = build_show_season_progress_from_history(history_items)

    logging.info("Building mismatch proposals from watching payload...")
    watching_rows = watching_payload.get("watching", [])
    for row_index, row in enumerate(watching_rows, start=1):
        logging.info("Processing watching entry: %s/%s", row_index, len(watching_rows))
        progress = row.get("progress")
        if not isinstance(progress, dict) or progress.get("is_finished") is not True:
            continue

        imdb_id, _ = resolve_or_skip_imdb(item=row, media_type="show", resolver=resolver)
        if not imdb_id:
            continue

        last_season = progress.get("last_watched_season")
        last_episode = progress.get("last_watched_episode")
        watched_dt = parse_iso_utc(row.get("last_viewed_at"))
        if not isinstance(last_season, int) or not isinstance(last_episode, int) or last_season <= 0 or last_episode <= 0:
            continue
        if watched_dt is None:
            continue
        last_viewed_at = dt_to_iso_z(watched_dt)

        if imdb_id not in season_count_cache:
            season_count_cache[imdb_id] = trakt_client.get_show_season_episode_counts(
                imdb_id,
                retries=retries,
                backoff_seconds=backoff_seconds,
                rate_limit_delay=rate_limit_delay,
            )
        season_counts = season_count_cache.get(imdb_id)
        if not isinstance(season_counts, dict):
            continue

        title: str = row.get("title", imdb_id) if isinstance(row.get("title"), str) else imdb_id
        trakt_season_count = max(season_counts)
        if last_season > trakt_season_count:
            logging.info(
                "Season count mismatch for %s (%s): KinoPub=%s Trakt=%s",
                title,
                imdb_id,
                last_season,
                trakt_season_count,
            )
            fingerprint = make_drop_fingerprint(imdb_id)
            if fingerprint not in seen_fingerprints:
                seen_fingerprints.add(fingerprint)
                proposals.append(
                    MismatchProposal(
                        action_type="drop_season_count_mismatch",
                        fingerprint=fingerprint,
                        imdb_id=imdb_id,
                        title=title,
                        season=last_season,
                        source_episode=last_episode,
                        inferred_episodes=[],
                        watched_at=last_viewed_at,
                        kinopub_season_count=last_season,
                        trakt_season_count=trakt_season_count,
                    )
                )
                stats.proposals_drop += 1
            continue

        show_history = history_season_progress.get(imdb_id, {})
        last_season_drop_needed = False

        for season in range(1, last_season + 1):
            if season == last_season:
                source_episode = last_episode
                season_watched_at = last_viewed_at
            else:
                season_progress = show_history.get(season)
                if season_progress is None:
                    logging.info(
                        "Skipping mismatch check for %s S%s: no KinoPub history for season",
                        imdb_id,
                        season,
                    )
                    continue
                source_episode, season_watched_at = season_progress

            trakt_max_episode = season_counts.get(season)
            if not isinstance(trakt_max_episode, int) or trakt_max_episode <= 0:
                continue

            if source_episode == trakt_max_episode:
                # Same final episode as Trakt for this season: not a mismatch.
                continue

            if source_episode > trakt_max_episode:
                logging.info(
                    "Episode progress beyond Trakt catalog for %s (%s): KinoPub S%sE%s > Trakt S%sE%s",
                    title,
                    imdb_id,
                    season,
                    source_episode,
                    season,
                    trakt_max_episode,
                )
                continue

            gap = trakt_max_episode - source_episode
            if gap <= max_gap:
                inferred_episodes = list(range(source_episode + 1, trakt_max_episode + 1))
                fingerprint = make_infer_fingerprint(imdb_id, season, source_episode, inferred_episodes)
                if fingerprint in seen_fingerprints:
                    continue
                seen_fingerprints.add(fingerprint)
                proposals.append(
                    MismatchProposal(
                        action_type="infer_tail_episodes",
                        fingerprint=fingerprint,
                        imdb_id=imdb_id,
                        title=title,
                        season=season,
                        source_episode=source_episode,
                        inferred_episodes=inferred_episodes,
                        watched_at=season_watched_at,
                    )
                )
                stats.proposals_infer += 1
                continue

            if season == last_season:
                last_season_drop_needed = True

        if not last_season_drop_needed:
            continue

        fingerprint = make_drop_fingerprint(imdb_id)
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        proposals.append(
            MismatchProposal(
                action_type="drop_finished_no_candidates",
                fingerprint=fingerprint,
                imdb_id=imdb_id,
                title=title,
                season=last_season,
                source_episode=last_episode,
                inferred_episodes=[],
                watched_at=last_viewed_at,
            )
        )
        stats.proposals_drop += 1

    return proposals, stats


def explain_mismatch_proposal(proposal: MismatchProposal) -> str:
    if proposal.action_type == "infer_tail_episodes":
        inferred = ",".join(str(number) for number in proposal.inferred_episodes)
        return (
            f"Finished show episode numbering differs: KinoPub ended at "
            f"S{proposal.season}E{proposal.source_episode}, inferred Trakt tail episodes [{inferred}] "
            f"for the same season."
        )
    if proposal.action_type == "drop_season_count_mismatch":
        return (
            f"Finished show season count differs: KinoPub has {proposal.kinopub_season_count} season(s), "
            f"Trakt has {proposal.trakt_season_count}; proposal is to hide the show as dropped."
        )
    return (
        f"Finished show has no safe tail-mismatch inference candidate at "
        f"S{proposal.season}E{proposal.source_episode}; proposal is to hide the show as dropped."
    )


def resolve_mismatch_proposal(
    proposal: MismatchProposal,
    current_decision: Optional[str],
    *,
    auto_approve: bool = False,
) -> Optional[str]:
    if current_decision in {"approved", "rejected"}:
        return current_decision

    if auto_approve:
        logging.info(
            "Auto-approving proposal for %s [%s]",
            proposal.title,
            proposal.fingerprint,
        )
        return "approved"

    if not sys.stdin.isatty():
        logging.warning(
            "Proposal %s is unresolved and no interactive terminal is available.",
            proposal.fingerprint,
        )
        return None

    prompt = (
        f"Resolve proposal for {proposal.title} [{proposal.fingerprint}] "
        "[A=approve (default), R=reject, D=defer]: "
    )
    while True:
        try:
            raw = input(prompt).strip().lower()
        except EOFError:
            logging.warning("Reached EOF while waiting for proposal decision: %s", proposal.fingerprint)
            return None
        if raw in {"", "a", "approve"}:
            return "approved"
        if raw in {"r", "reject"}:
            return "rejected"
        if raw in {"d", "defer", "u", "unresolved"}:
            return None
        print("Please enter A, R, or D.")


def get_oauth_token_via_device_flow(
    *,
    client_id: str,
    client_secret: str,
    trakt_base_url: str,
    timeout_seconds: int,
) -> Tuple[str, str, int]:
    """Run Trakt Device Code Flow and return (access_token, refresh_token, expires_at)."""
    base_url = trakt_base_url.rstrip("/")
    device_code_url = f"{base_url}/oauth/device/code"
    device_token_url = f"{base_url}/oauth/device/token"

    try:
        code_response = requests.post(
            device_code_url,
            json={"client_id": client_id},
            timeout=20,
        )
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
            try:
                token_payload = token_response.json()
            except ValueError as exc:
                raise RuntimeError(f"Invalid token response JSON: {exc}") from exc

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
            # authorization_pending
            time.sleep(poll_interval)
            continue
        if token_response.status_code == 429:
            # slow_down
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

        raise RuntimeError(
            f"Unexpected device auth response: {token_response.status_code} {token_response.text}"
        )

    raise RuntimeError("Device auth timed out before authorization completed")


def load_history(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    history = payload.get("history") if isinstance(payload, dict) else None
    if not isinstance(history, list):
        raise ValueError(f"Invalid history payload in {path}: expected top-level history[]")
    return [item for item in history if isinstance(item, dict)]


def load_watchlist(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    root = payload.get("watchlist") if isinstance(payload, dict) else None
    if not isinstance(root, dict):
        raise ValueError(f"Invalid watchlist payload in {path}: expected top-level watchlist")

    movies = root.get("movies")
    shows = root.get("shows")
    if not isinstance(movies, list) or not isinstance(shows, list):
        raise ValueError(f"Invalid watchlist payload in {path}: expected watchlist.movies[] and watchlist.shows[]")

    return {
        "movies": [item for item in movies if isinstance(item, dict)],
        "shows": [item for item in shows if isinstance(item, dict)],
    }


def load_watching(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    watching = payload.get("watching") if isinstance(payload, dict) else None
    dropped = payload.get("dropped") if isinstance(payload, dict) else None
    if not isinstance(watching, list) or not isinstance(dropped, list):
        raise ValueError(f"Invalid watching payload in {path}: expected top-level watching[] and dropped[]")

    return {
        "watching": [item for item in watching if isinstance(item, dict)],
        "dropped": [item for item in dropped if isinstance(item, dict)],
    }


def resolve_or_skip_imdb(
    *,
    item: Dict[str, Any],
    media_type: str,
    resolver: TMDBResolver,
) -> Tuple[Optional[str], bool]:
    direct = normalize_imdb_id(item.get("imdb_id"))
    if direct:
        return direct, False

    year = item.get("year")
    normalized_year = int(year) if isinstance(year, int) else None
    resolved = resolver.resolve_imdb_id(
        title=item.get("title"),
        original_title=item.get("original_title"),
        year=normalized_year,
        media_type=media_type,
    )
    if resolved:
        return resolved, True
    return None, False


def build_history_payloads(
    history_items: List[Dict[str, Any]],
    *,
    batch_size: int,
    resolver: TMDBResolver,
) -> Tuple[List[Dict[str, Any]], BuildStats]:
    stats = BuildStats()
    dedup_keys = set()
    unresolved_keys = set()
    movies: List[Dict[str, Any]] = []
    shows: List[Dict[str, Any]] = []

    sorted_items = sorted(
        history_items,
        key=lambda row: parse_iso_utc(row.get("watched_at")) or datetime.fromtimestamp(0, tz=timezone.utc),
    )

    for row in sorted_items:
        item_type = row.get("type")
        if item_type not in {"movie", "show"}:
            stats.skipped += 1
            logging.warning("Skipping history item with unsupported type: %s", item_type)
            continue

        watched_dt = parse_iso_utc(row.get("watched_at"))
        if watched_dt is None:
            stats.skipped += 1
            logging.warning("Skipping history item with invalid watched_at: %s", row.get("watched_at"))
            continue
        watched_at = dt_to_iso_z(watched_dt)

        imdb_id, enriched = resolve_or_skip_imdb(item=row, media_type=item_type, resolver=resolver)
        if not imdb_id:
            stats.skipped += 1
            unresolved_key = (item_type, row.get("title"), row.get("year"), row.get("kinopoisk_id"))
            if unresolved_key not in unresolved_keys:
                unresolved_keys.add(unresolved_key)
                logging.warning(
                    "Skipping history %s item without resolvable IMDb ID: title=%s year=%s kinopoisk_id=%s",
                    item_type,
                    row.get("title"),
                    row.get("year"),
                    row.get("kinopoisk_id"),
                )
            continue
        if enriched:
            stats.enriched += 1

        if item_type == "movie":
            dedup_key = ("movie", imdb_id, watched_at)
            if dedup_key in dedup_keys:
                continue
            dedup_keys.add(dedup_key)
            movies.append({"ids": {"imdb": imdb_id}, "watched_at": watched_at})
            stats.prepared += 1
            continue

        season = row.get("season")
        episode = row.get("episode")
        if not isinstance(season, int) or not isinstance(episode, int) or season <= 0 or episode <= 0:
            stats.skipped += 1
            logging.warning(
                "Skipping show history row with invalid season/episode: imdb=%s season=%s episode=%s",
                imdb_id,
                season,
                episode,
            )
            continue

        dedup_key = ("show", imdb_id, season, episode, watched_at)
        if dedup_key in dedup_keys:
            continue
        dedup_keys.add(dedup_key)

        shows.append(
            {
                "ids": {"imdb": imdb_id},
                "seasons": [
                    {
                        "number": season,
                        "episodes": [{"number": episode, "watched_at": watched_at}],
                    }
                ],
            }
        )
        stats.prepared += 1

    payloads: List[Dict[str, Any]] = []
    for part in chunked(movies, batch_size):
        payloads.append({"movies": part})
    for part in chunked(shows, batch_size):
        payloads.append({"shows": part})
    return payloads, stats


def build_watchlist_payloads(
    watchlist: Dict[str, List[Dict[str, Any]]],
    *,
    batch_size: int,
    resolver: TMDBResolver,
) -> Tuple[List[Dict[str, Any]], BuildStats]:
    stats = BuildStats()
    dedup_keys = set()
    unresolved_keys = set()
    movies: List[Dict[str, Any]] = []
    shows: List[Dict[str, Any]] = []

    for media_type in ["movie", "show"]:
        key = "movies" if media_type == "movie" else "shows"
        for row in watchlist[key]:
            imdb_id, enriched = resolve_or_skip_imdb(item=row, media_type=media_type, resolver=resolver)
            if not imdb_id:
                stats.skipped += 1
                unresolved_key = (media_type, row.get("title"), row.get("year"), row.get("kinopoisk_id"))
                if unresolved_key not in unresolved_keys:
                    unresolved_keys.add(unresolved_key)
                    logging.warning(
                        "Skipping watchlist %s item without resolvable IMDb ID: title=%s year=%s kinopoisk_id=%s",
                        media_type,
                        row.get("title"),
                        row.get("year"),
                        row.get("kinopoisk_id"),
                    )
                continue
            if enriched:
                stats.enriched += 1

            dedup_key = (media_type, imdb_id)
            if dedup_key in dedup_keys:
                continue
            dedup_keys.add(dedup_key)

            if media_type == "movie":
                movies.append({"ids": {"imdb": imdb_id}})
            else:
                shows.append({"ids": {"imdb": imdb_id}})
            stats.prepared += 1

    payloads: List[Dict[str, Any]] = []
    for part in chunked(movies, batch_size):
        payloads.append({"movies": part})
    for part in chunked(shows, batch_size):
        payloads.append({"shows": part})
    return payloads, stats


def build_dropped_show_remove_payloads(
    watching_payload: Dict[str, List[Dict[str, Any]]],
    *,
    batch_size: int,
    resolver: TMDBResolver,
) -> Tuple[List[Dict[str, Any]], BuildStats]:
    stats = BuildStats()
    dedup_keys = set()
    unresolved_keys = set()
    shows: List[Dict[str, Any]] = []

    for row in watching_payload.get("dropped", []):
        imdb_id, enriched = resolve_or_skip_imdb(item=row, media_type="show", resolver=resolver)
        if not imdb_id:
            unresolved_key = ("show", row.get("title"), row.get("year"), row.get("kinopoisk_id"))
            if unresolved_key not in unresolved_keys:
                unresolved_keys.add(unresolved_key)
                stats.skipped += 1
                logging.warning(
                    "Skipping dropped candidate show without resolvable IMDb ID: title=%s year=%s kinopoisk_id=%s",
                    row.get("title"),
                    row.get("year"),
                    row.get("kinopoisk_id"),
                )
            continue

        if enriched:
            stats.enriched += 1

        dedup_key = ("show", imdb_id)
        if dedup_key in dedup_keys:
            continue
        dedup_keys.add(dedup_key)

        shows.append({"ids": {"imdb": imdb_id}})
        stats.prepared += 1

    payloads: List[Dict[str, Any]] = []
    for part in chunked(shows, batch_size):
        payloads.append({"shows": part})
    return payloads, stats


def build_finished_watching_history_payloads(
    watching_payload: Dict[str, List[Dict[str, Any]]],
    *,
    batch_size: int,
    resolver: TMDBResolver,
) -> Tuple[List[Dict[str, Any]], BuildStats]:
    stats = BuildStats()
    dedup_keys = set()
    unresolved_keys = set()
    shows: List[Dict[str, Any]] = []

    for row in watching_payload.get("watching", []):
        progress = row.get("progress")
        if not isinstance(progress, dict):
            continue
        if progress.get("is_finished") is not True:
            continue

        imdb_id, enriched = resolve_or_skip_imdb(item=row, media_type="show", resolver=resolver)
        if not imdb_id:
            unresolved_key = ("show", row.get("title"), row.get("year"), row.get("kinopoisk_id"))
            if unresolved_key not in unresolved_keys:
                unresolved_keys.add(unresolved_key)
                stats.skipped += 1
                logging.warning(
                    "Skipping finished watching show without resolvable IMDb ID: title=%s year=%s kinopoisk_id=%s",
                    row.get("title"),
                    row.get("year"),
                    row.get("kinopoisk_id"),
                )
            continue

        if enriched:
            stats.enriched += 1

        season = progress.get("last_watched_season")
        episode = progress.get("last_watched_episode")
        if not isinstance(season, int) or not isinstance(episode, int) or season <= 0 or episode <= 0:
            stats.skipped += 1
            logging.warning(
                "Skipping finished watching show with invalid season/episode: imdb=%s season=%s episode=%s",
                imdb_id,
                season,
                episode,
            )
            continue

        watched_dt = parse_iso_utc(row.get("last_viewed_at"))
        if watched_dt is None:
            stats.skipped += 1
            logging.warning(
                "Skipping finished watching show with invalid last_viewed_at: imdb=%s last_viewed_at=%s",
                imdb_id,
                row.get("last_viewed_at"),
            )
            continue
        watched_at = dt_to_iso_z(watched_dt)

        dedup_key = ("show", imdb_id, season, episode, watched_at)
        if dedup_key in dedup_keys:
            continue
        dedup_keys.add(dedup_key)

        shows.append(
            {
                "ids": {"imdb": imdb_id},
                "seasons": [
                    {
                        "number": season,
                        "episodes": [{"number": episode, "watched_at": watched_at}],
                    }
                ],
            }
        )
        stats.prepared += 1

    payloads: List[Dict[str, Any]] = []
    for part in chunked(shows, batch_size):
        payloads.append({"shows": part})
    return payloads, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import KinoPub export data into Trakt using PyTrakt.")
    parser.add_argument("--history-file", type=Path, default=DEFAULT_HISTORY_FILE)
    parser.add_argument("--watchlist-file", type=Path, default=DEFAULT_WATCHLIST_FILE)
    parser.add_argument("--watching-file", type=Path, default=DEFAULT_WATCHING_FILE)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--token-cache-file", type=Path, default=DEFAULT_TOKEN_CACHE_FILE)
    parser.add_argument("--mismatch-approve-cache-file", type=Path, default=DEFAULT_MISMATCH_APPROVE_CACHE_FILE)

    parser.add_argument("--trakt-client-id", type=str, default=os.environ.get("TRAKT_CLIENT_ID"))
    parser.add_argument("--trakt-client-secret", type=str, default=os.environ.get("TRAKT_CLIENT_SECRET"))
    parser.add_argument("--trakt-base-url", type=str, default=DEFAULT_TRAKT_BASE_URL)
    parser.add_argument("--device-auth-timeout", type=int, default=DEFAULT_DEVICE_AUTH_TIMEOUT)

    parser.add_argument("--tmdb-api-key", type=str, default=os.environ.get("TMDB_API_KEY"))
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--retry-attempts", type=int, default=DEFAULT_RETRY_ATTEMPTS)
    parser.add_argument("--backoff-seconds", type=float, default=DEFAULT_BACKOFF_SECONDS)
    parser.add_argument("--rate-limit-delay", type=float, default=DEFAULT_RATE_LIMIT_DELAY)
    parser.add_argument(
        "--mismatch-mode",
        type=str,
        default=DEFAULT_MISMATCH_MODE,
        choices=["off", "approve"],
    )
    parser.add_argument("--mismatch-max-gap", type=int, default=DEFAULT_MISMATCH_MAX_GAP)
    parser.add_argument("--mismatch-approve-cache-clean", action="store_true")
    parser.add_argument(
        "--mismatch-auto-approve",
        action="store_true",
        help="Approve all unresolved mismatch proposals without interactive prompts.",
    )

    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--log-level",
        type=str,
        default=os.environ.get("TRAKT_IMPORT_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser.parse_args()


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
    if not isinstance(payload, dict):
        return None
    return payload


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
    base_url = trakt_base_url.rstrip("/")
    token_url = f"{base_url}/oauth/token"

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


def write_state(
    path: Path,
    *,
    dry_run: bool,
    history_stats: BuildStats,
    finished_watching_stats: BuildStats,
    watchlist_stats: BuildStats,
    dropped_stats: BuildStats,
    history_imported: int,
    finished_watching_marked: int,
    watchlist_imported: int,
    dropped_hidden: int,
    mismatch_stats: MismatchStats,
    mismatch_mode: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": dt_to_iso_z(datetime.now(tz=timezone.utc)),
        "dry_run": dry_run,
        "history": {
            "prepared": history_stats.prepared,
            "skipped": history_stats.skipped,
            "enriched": history_stats.enriched,
            "imported": history_imported,
        },
        "finished_watching": {
            "prepared": finished_watching_stats.prepared,
            "skipped": finished_watching_stats.skipped,
            "enriched": finished_watching_stats.enriched,
            "marked": finished_watching_marked,
        },
        "watchlist": {
            "prepared": watchlist_stats.prepared,
            "skipped": watchlist_stats.skipped,
            "enriched": watchlist_stats.enriched,
            "imported": watchlist_imported,
        },
        "dropped": {
            "prepared": dropped_stats.prepared,
            "skipped": dropped_stats.skipped,
            "enriched": dropped_stats.enriched,
            "hidden": dropped_hidden,
        },
        "mismatch": {
            "mode": mismatch_mode,
            "heuristic_version": MISMATCH_HEURISTIC_VERSION,
            "proposals_infer": mismatch_stats.proposals_infer,
            "proposals_drop": mismatch_stats.proposals_drop,
            "applied_infer": mismatch_stats.applied_infer,
            "applied_drop": mismatch_stats.applied_drop,
            "skipped_unapproved": mismatch_stats.skipped_unapproved,
            "skipped_rejected": mismatch_stats.skipped_rejected,
            "unresolved": mismatch_stats.unresolved,
        },
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def main() -> int:
    args = parse_args()
    setup_logging(args.log_level)

    if not args.dry_run and not args.trakt_client_id:
        logging.error("Missing Trakt client ID. Set TRAKT_CLIENT_ID or pass --trakt-client-id.")
        return 1
    if not args.dry_run and not args.trakt_client_secret:
        logging.error("Missing Trakt client secret. Set TRAKT_CLIENT_SECRET or pass --trakt-client-secret.")
        return 1
    if args.device_auth_timeout <= 0:
        logging.error("--device-auth-timeout must be greater than 0")
        return 1

    if args.batch_size <= 0:
        logging.error("--batch-size must be greater than 0")
        return 1
    if args.mismatch_max_gap <= 0:
        logging.error("--mismatch-max-gap must be greater than 0")
        return 1

    try:
        history_items = load_history(args.history_file)
        watchlist = load_watchlist(args.watchlist_file)
        watching_payload = load_watching(args.watching_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logging.error("Input loading failed: %s", exc)
        return 1

    resolver = TMDBResolver(api_key=args.tmdb_api_key)

    history_payloads, history_stats = build_history_payloads(
        history_items,
        batch_size=args.batch_size,
        resolver=resolver,
    )
    watchlist_payloads, watchlist_stats = build_watchlist_payloads(
        watchlist,
        batch_size=args.batch_size,
        resolver=resolver,
    )
    dropped_remove_payloads, dropped_stats = build_dropped_show_remove_payloads(
        watching_payload,
        batch_size=args.batch_size,
        resolver=resolver,
    )
    finished_watching_payloads, finished_watching_stats = build_finished_watching_history_payloads(
        watching_payload,
        batch_size=args.batch_size,
        resolver=resolver,
    )

    logging.info(
        "Prepared history import: %s item(s), skipped=%s, imdb_enriched=%s, batches=%s",
        history_stats.prepared,
        history_stats.skipped,
        history_stats.enriched,
        len(history_payloads),
    )
    logging.info(
        "Prepared finished-watching marks: %s item(s), skipped=%s, imdb_enriched=%s, batches=%s",
        finished_watching_stats.prepared,
        finished_watching_stats.skipped,
        finished_watching_stats.enriched,
        len(finished_watching_payloads),
    )
    logging.info(
        "Prepared watchlist import: %s item(s), skipped=%s, imdb_enriched=%s, batches=%s",
        watchlist_stats.prepared,
        watchlist_stats.skipped,
        watchlist_stats.enriched,
        len(watchlist_payloads),
    )
    logging.info(
        "Prepared dropped-show removals: %s item(s), skipped=%s, imdb_enriched=%s, batches=%s",
        dropped_stats.prepared,
        dropped_stats.skipped,
        dropped_stats.enriched,
        len(dropped_remove_payloads),
    )

    history_imported = 0
    finished_watching_marked = 0
    watchlist_imported = 0
    dropped_hidden = 0
    mismatch_stats = MismatchStats()

    if args.dry_run:
        write_state(
            args.state_file,
            dry_run=True,
            history_stats=history_stats,
            finished_watching_stats=finished_watching_stats,
            watchlist_stats=watchlist_stats,
            dropped_stats=dropped_stats,
            history_imported=0,
            finished_watching_marked=0,
            watchlist_imported=0,
            dropped_hidden=0,
            mismatch_stats=mismatch_stats,
            mismatch_mode=args.mismatch_mode,
        )
        logging.info("Dry-run complete. State written to %s", args.state_file)
        return 0

    try:
        access_token, refresh_token, expires_at = get_cached_or_device_tokens(
            client_id=args.trakt_client_id,
            client_secret=args.trakt_client_secret,
            trakt_base_url=args.trakt_base_url,
            timeout_seconds=args.device_auth_timeout,
            token_cache_file=args.token_cache_file,
        )
    except RuntimeError as exc:
        logging.error("Device auth failed: %s", exc)
        return 1

    trakt_client = TraktClient(
        client_id=args.trakt_client_id,
        access_token=access_token,
        client_secret=args.trakt_client_secret,
        base_url=args.trakt_base_url,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )

    if args.mismatch_mode == "approve":
        if args.mismatch_approve_cache_clean:
            save_mismatch_approval_cache(args.mismatch_approve_cache_file, {})
            logging.info("Cleared mismatch approval cache at %s", args.mismatch_approve_cache_file)

        approvals = load_mismatch_approval_cache(args.mismatch_approve_cache_file)
        proposals, mismatch_stats = build_mismatch_proposals(
            watching_payload,
            history_items,
            resolver=resolver,
            trakt_client=trakt_client,
            retries=args.retry_attempts,
            backoff_seconds=args.backoff_seconds,
            rate_limit_delay=args.rate_limit_delay,
            max_gap=args.mismatch_max_gap,
        )

        finished_items = flatten_payload_items(finished_watching_payloads, "shows")
        dropped_items = flatten_payload_items(dropped_remove_payloads, "shows")

        for proposal in proposals:
            decision_payload = approvals.get(proposal.fingerprint)
            decision = decision_payload.get("decision") if isinstance(decision_payload, dict) else None

            logging.info("Mismatch explanation: %s", explain_mismatch_proposal(proposal))

            logging.info(
                "Mismatch proposal: action=%s title=%s imdb=%s season=%s source_episode=%s inferred=%s fingerprint=%s decision=%s",
                proposal.action_type,
                proposal.title,
                proposal.imdb_id,
                proposal.season,
                proposal.source_episode,
                proposal.inferred_episodes,
                proposal.fingerprint,
                decision if isinstance(decision, str) else "unapproved",
            )

            decision = resolve_mismatch_proposal(
                proposal,
                decision if isinstance(decision, str) else None,
                auto_approve=args.mismatch_auto_approve,
            )
            if decision in {"approved", "rejected"}:
                approvals[proposal.fingerprint] = {
                    "decision": decision,
                    "decided_at": dt_to_iso_z(datetime.now(tz=timezone.utc)),
                    "action_type": proposal.action_type,
                }

            if decision == "approved":
                if proposal.action_type == "infer_tail_episodes" and proposal.watched_at:
                    season_number = proposal.season if isinstance(proposal.season, int) else None
                    source_episode = proposal.source_episode if isinstance(proposal.source_episode, int) else None
                    if season_number is None or source_episode is None:
                        continue
                    for inferred_episode in proposal.inferred_episodes:
                        finished_items.append(
                            {
                                "ids": {"imdb": proposal.imdb_id},
                                "seasons": [
                                    {
                                        "number": season_number,
                                        "episodes": [
                                            {
                                                "number": inferred_episode,
                                                "watched_at": proposal.watched_at,
                                            }
                                        ],
                                    }
                                ],
                            }
                        )
                        mismatch_stats.applied_infer += 1
                elif proposal.action_type in {"drop_finished_no_candidates", "drop_season_count_mismatch"}:
                    dropped_items.append({"ids": {"imdb": proposal.imdb_id}})
                    mismatch_stats.applied_drop += 1
                continue

            if decision == "rejected":
                mismatch_stats.skipped_rejected += 1
            else:
                mismatch_stats.skipped_unapproved += 1
                mismatch_stats.unresolved += 1

        finished_items = dedupe_show_history_items(finished_items)
        dropped_items = dedupe_show_id_items(dropped_items)
        finished_watching_payloads = build_payloads_from_items(finished_items, "shows", args.batch_size)
        dropped_remove_payloads = build_payloads_from_items(dropped_items, "shows", args.batch_size)

        save_mismatch_approval_cache(args.mismatch_approve_cache_file, approvals)

        logging.info(
            "Mismatch summary: infer_proposals=%s drop_proposals=%s infer_applied=%s drop_applied=%s skipped_unapproved=%s skipped_rejected=%s unresolved=%s",
            mismatch_stats.proposals_infer,
            mismatch_stats.proposals_drop,
            mismatch_stats.applied_infer,
            mismatch_stats.applied_drop,
            mismatch_stats.skipped_unapproved,
            mismatch_stats.skipped_rejected,
            mismatch_stats.unresolved,
        )

        if mismatch_stats.unresolved > 0:
            logging.error(
                "Found %s unresolved mismatch proposal(s). Aborting before any Trakt sync.",
                mismatch_stats.unresolved,
            )
            write_state(
                args.state_file,
                dry_run=False,
                history_stats=history_stats,
                finished_watching_stats=finished_watching_stats,
                watchlist_stats=watchlist_stats,
                dropped_stats=dropped_stats,
                history_imported=0,
                finished_watching_marked=0,
                watchlist_imported=0,
                dropped_hidden=0,
                mismatch_stats=mismatch_stats,
                mismatch_mode=args.mismatch_mode,
            )
            logging.info("State written to %s", args.state_file)
            return 1

    try:
        logging.info("Starting Trakt sync/history import (%s batch(es))", len(history_payloads))
        for index, payload in enumerate(history_payloads, start=1):
            batch_items = sum(len(v) for v in payload.values() if isinstance(v, list))
            logging.info("Posting sync/history batch %s/%s (%s item(s))", index, len(history_payloads), batch_items)
            response = trakt_client.post_with_retry(
                "sync/history",
                payload,
                retries=args.retry_attempts,
                backoff_seconds=args.backoff_seconds,
                rate_limit_delay=args.rate_limit_delay,
            )
            added = extract_added_count(response)
            history_imported += added if added else sum(len(v) for v in payload.values() if isinstance(v, list))

        logging.info("Starting Trakt sync/history finished-watching marks (%s batch(es))", len(finished_watching_payloads))
        for index, payload in enumerate(finished_watching_payloads, start=1):
            batch_items = sum(len(v) for v in payload.values() if isinstance(v, list))
            logging.info(
                "Posting sync/history finished-watching batch %s/%s (%s item(s))",
                index,
                len(finished_watching_payloads),
                batch_items,
            )
            response = trakt_client.post_with_retry(
                "sync/history",
                payload,
                retries=args.retry_attempts,
                backoff_seconds=args.backoff_seconds,
                rate_limit_delay=args.rate_limit_delay,
            )
            added = extract_added_count(response)
            finished_watching_marked += added if added else sum(len(v) for v in payload.values() if isinstance(v, list))

        logging.info("Starting Trakt sync/watchlist import (%s batch(es))", len(watchlist_payloads))
        for index, payload in enumerate(watchlist_payloads, start=1):
            batch_items = sum(len(v) for v in payload.values() if isinstance(v, list))
            logging.info("Posting sync/watchlist batch %s/%s (%s item(s))", index, len(watchlist_payloads), batch_items)
            response = trakt_client.post_with_retry(
                "sync/watchlist",
                payload,
                retries=args.retry_attempts,
                backoff_seconds=args.backoff_seconds,
                rate_limit_delay=args.rate_limit_delay,
            )
            added = extract_added_count(response)
            watchlist_imported += added if added else sum(len(v) for v in payload.values() if isinstance(v, list))

        logging.info("Starting Trakt users/hidden/dropped sync for dropped shows (%s batch(es))", len(dropped_remove_payloads))
        for index, payload in enumerate(dropped_remove_payloads, start=1):
            batch_items = sum(len(v) for v in payload.values() if isinstance(v, list))
            logging.info(
                "Posting users/hidden/dropped batch %s/%s (%s item(s))",
                index,
                len(dropped_remove_payloads),
                batch_items,
            )
            response = trakt_client.post_with_retry(
                "users/hidden/dropped",
                payload,
                retries=args.retry_attempts,
                backoff_seconds=args.backoff_seconds,
                rate_limit_delay=args.rate_limit_delay,
            )
            deleted = extract_deleted_count(response)
            dropped_hidden += deleted if deleted else sum(len(v) for v in payload.values() if isinstance(v, list))
    except trakt_errors.OAuthException as exc:
        logging.error("Trakt authentication failed: %s", exc)
        return 1
    except trakt_errors.TraktException as exc:
        logging.error("Trakt request failed: %s", exc)
        return 1

    write_state(
        args.state_file,
        dry_run=False,
        history_stats=history_stats,
        finished_watching_stats=finished_watching_stats,
        watchlist_stats=watchlist_stats,
        dropped_stats=dropped_stats,
        history_imported=history_imported,
        finished_watching_marked=finished_watching_marked,
        watchlist_imported=watchlist_imported,
        dropped_hidden=dropped_hidden,
        mismatch_stats=mismatch_stats,
        mismatch_mode=args.mismatch_mode,
    )

    logging.info("Trakt import completed successfully.")
    logging.info("Imported history events: %s", history_imported)
    logging.info("Marked finished watching shows as watched: %s", finished_watching_marked)
    logging.info("Imported watchlist items: %s", watchlist_imported)
    logging.info("Hidden dropped shows in Trakt: %s", dropped_hidden)
    logging.info("State written to %s", args.state_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
