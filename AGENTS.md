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
- python kinopub-exporter.py --history-file data/history.json
- python kinopub-exporter.py --base-url https://api.service-kp.com

Notes:
- There is currently no test suite or lint configuration in this repository.
- Validate behavior by running the exporter and checking generated JSON files.

## Code Map

Main implementation:
- [kinopub-exporter.py](kinopub-exporter.py): API client, pagination, filtering, incremental/full sync, JSON output.

Not part of exporter flow:
- [main.py](main.py): placeholder script.

Generated outputs:
- [data/history.json](data/history.json)
- [data/watchlist.json](data/watchlist.json)
- [data/currently_watching.json](data/currently_watching.json)

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
- Keep requests-cache and Last-Modified handling unless changing cache strategy intentionally.

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
