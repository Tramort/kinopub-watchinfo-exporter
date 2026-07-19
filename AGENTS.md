# AGENTS Guide

This repository is a single-purpose Python exporter for KinoPub watch data.

Use this file as the first-stop operational guide. For product and format details, see:
- [README.md](README.md)
- [SPEC.md](SPEC.md)

## What to Run

Prerequisites:
- Python 3.9+
- KINOPUB_TOKEN environment variable (or pass --token)

Primary run command:
- python kinopub-exporter.py

Useful variants:
- python kinopub-exporter.py --full
- python kinopub-exporter.py --since 2026-07-05T12:00:00Z
- python kinopub-exporter.py --cache
- python kinopub-exporter.py --history-file data/history.json
- python kinopub-exporter.py --base-url https://api.service-kp.com
- python traktv-importer.py --dry-run
- python trakt-nextup.py --dry-run

Docker:
- Published image: `ghcr.io/tramort/kinopub-watchinfo-exporter` (`latest`, semver tags, SHA).
- docker build -t ghcr.io/tramort/kinopub-watchinfo-exporter:latest .
- docker run --rm --env-file .env -v "$PWD/data:/app/data" ghcr.io/tramort/kinopub-watchinfo-exporter:latest kinopub-exporter.py
- Periodic: set `CRON_SCHEDULE` (e.g. `0 */6 * * *`) — same image, any script as the command.
- docker compose up -d --build
- Scheduled `traktv-importer.py` needs `--mismatch-auto-approve` (no interactive prompts).

Notes:
- There is currently no test suite or lint configuration in this repository.
- CI: `.github/workflows/docker.yml` builds (PRs) and publishes multi-arch images to GHCR (main / `v*` tags).
- Validate behavior by running the exporter and checking generated JSON files.

## Code Map

Main implementation:
- [kinopub-exporter.py](kinopub-exporter.py): API client, pagination, filtering, incremental/full sync, JSON output.
- [traktv-importer.py](traktv-importer.py): import exported watch data into Trakt.tv.
- [trakt-nextup.py](trakt-nextup.py): sync Trakt in-progress shows to a custom list for Sonarr.
- [Dockerfile](Dockerfile) / [docker-entrypoint.sh](docker-entrypoint.sh): image entrypoint; one-shot or `CRON_SCHEDULE` via supercronic.
- [docker-compose.yml](docker-compose.yml): example scheduled services.
- [.github/workflows/docker.yml](.github/workflows/docker.yml): build/push multi-arch image to GHCR.

Not part of exporter flow:
- [main.py](main.py): placeholder script.

Generated outputs:
- [data/history.json](data/history.json)
- [data/watchlist.json](data/watchlist.json)
- [data/watching.json](data/watching.json)
- [data/trakt_nextup_state.json](data/trakt_nextup_state.json)

## Project Conventions

Data and time:
- Use UTC ISO-8601 timestamps with Z suffix.
- Keep output schema aligned with [SPEC.md](SPEC.md).

ID handling:
- Prefer imdb_id as primary external identifier.
- kinopoisk_id may be present but is not a substitute for missing imdb_id.
- Keep IMDb normalization and validation behavior intact unless explicitly requested.

Media classification:
- Movie and show types are controlled by MOVIE_TYPES and SHOW_TYPES constants.
- Keep season 0 (specials) excluded unless requirements change.

Filtering and deduplication:
- Preserve 24-hour deduplication for movie multi-part duplicates.
- Preserve trailer/bonus filtering logic for history items.

Reliability and API behavior:
- Keep retry + backoff semantics.
- Keep API version detection and prefix fallback logic.
- HTTP responses are always written to requests-cache; read from cache only when `--cache` is set. Without `--cache`, requests use force_refresh (fresh network data).

## Edit Guidance For Agents

When implementing changes:
- Make focused edits in [kinopub-exporter.py](kinopub-exporter.py), avoid refactoring unrelated areas.
- Preserve command-line flags and defaults unless the task requires changes.
- Keep output file names and top-level JSON keys stable unless requested.
- If changing output shape, update [SPEC.md](SPEC.md) in the same change.

Before finishing:
- Run the relevant exporter command for the changed path (full or incremental).
- Check output JSON validity and key structure.
- Confirm no regression in filtering, deduplication, and timestamp logic.

## Common Pitfalls

- Missing/invalid token causes authorization failures.
- API endpoints may differ by version, so fallback behavior is important.
- Missing IMDb IDs are common; avoid silently emitting invalid identifiers.
- Overly broad filter changes can re-introduce trailer noise or duplicate movie entries.
