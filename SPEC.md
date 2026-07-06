# Kinopub-watchinfo-exporter

Goals: export history, watchlist, and watching series data from kinopub.

API doc: https://kinoapi.com
Note: API may be outdated; implement version detection and fallback logic

Supported types: movie, 3d, series (with episode/season granularity)

## Requirements:
### Output examples
Watching history:
```
{
  "history": [
    {
      "title": "Черное зеркало",
      "original_title": "Black Mirror",
      "kinopub_id": 101,
      "imdb_id": "tt2085059",
      "kinopoisk_id": 655800,
      "year": 2011,
      "type": "show",
      "season": 1,
      "episode": 1,
      "watched_at": "2026-05-10T21:00:00Z"
    },
    {
      "title": "Интерстеллар",
      "original_title": "Interstellar",
      "kinopub_id": 102,
      "imdb_id": "tt0816692",
      "kinopoisk_id": 258687,
      "year": 2014,
      "type": "movie",
      "is_3d": false,
      "season": null,
      "episode": null,
      "watched_at": "2026-05-12T18:30:00Z"
    },
    {
      "title": "Аватар",
      "original_title": "Avatar",
      "kinopub_id": 103,
      "imdb_id": "tt0499549",
      "kinopoisk_id": 251733,
      "year": 2009,
      "type": "movie",
      "is_3d": true,
      "season": null,
      "episode": null,
      "watched_at": "2026-05-13T19:45:00Z"
    }
  ]
}
```
Watchilist:
```
{
  "watchlist": {
    "movies": [
      {
        "title": "Начало",
        "original_title": "Inception",
        "kinopub_id": 201,
        "imdb_id": "tt1375666",
        "kinopoisk_id": 447301,
        "year": 2010,
        "added_at": "2026-07-05T12:00:00Z"
      },
      {
        "title": "Аватар",
        "original_title": "Avatar",
        "kinopub_id": 202,
        "imdb_id": "tt0499549",
        "kinopoisk_id": 251733,
        "year": 2009,
        "added_at": "2026-07-05T12:03:00Z"
      }
    ],
    "shows": [
      {
        "title": "Во все тяжкие",
        "original_title": "Breaking Bad",
        "kinopub_id": 203,
        "imdb_id": "tt0903747",
        "kinopoisk_id": 404900,
        "year": 2008,
        "added_at": "2026-07-05T12:05:00Z"
      }
    ]
  }
}
```
Watching (only series):
```
{
  "watching": [
    {
      "title": "Пацаны",
      "original_title": "The Boys",
      "kinopub_id": 301,
      "imdb_id": "tt1190634",
      "kinopoisk_id": 1113943,
      "year": 2019,
      "progress": {
        "last_watched_season": 4,
        "last_watched_episode": 8,
        "is_finished": false
      },
      "last_viewed_at": "2026-07-01T20:30:00Z"
    }
  ],
  "dropped": []
}
```

## Usage

Sync Modes:
   - Default: Incremental (track changes since last_sync_timestamp)
   - Optional: Full export (--full flag)

## Implementation Notes

- kinopub history api uses pagination; implement automatic pagination handling
- cache raw API responses to avoid redundant requests and reduce load on the API (requests-cache)
- Error Handling:
  - Skip unsupported types (log warnings)
  - Retry failed API calls (3 attempts with backoff)
  - Validate IMDb IDs before export; if IMDb is missing/invalid but Kinopoisk ID exists, keep the item with `imdb_id: null`
  - Keep items even when both IMDb and Kinopoisk IDs are missing; warn if required metadata (`original_title`, `year`) is missing
- The is_finished Marker: Tracking whether a show is fully completed helps platforms like Simkl automatically archive
  it, preventing your active "Watching" list from getting cluttered with dead weight.
- Multi-part Movie DuplicatesMovies split into multiple video files on Kino.pub register as multiple full views in your
  history log. They require deduplication within a 24-hour window.
- Bonus Material Pollution: Clicking on short clips, trailers, or "making-of" videos registers as a full movie watch
  event. These must be filtered out by keywords or duration thresholds.
- 3D Separation: KinoPub type 3d is exported as a separate history record using `is_3d: true`.
  In watchlist output, `3d` titles are grouped under `watchlist.movies`.
  Movie deduplication is isolated by `(external_id, is_3d)` where `external_id = imdb_id` or `kp:<kinopoisk_id>` fallback,
  so 2D and 3D entries do not suppress each other and Kinopoisk-only items deduplicate correctly.
- Trakt Compatibility: Trakt.tv does not support 3D as a separate item type.
  Downstream Trakt import should map 3D watches to standard movie history items (optionally preserving local `is_3d` metadata).
- Season 0 (Specials) Mismatch: Special episode numbering on Kino.pub rarely matches official databases (TMDB/TVDB).
  It is safest to skip season == 0 entirely to avoid messed-up history syncs.
- Titles: exporter writes `title` and `original_title`; when explicit original title is unavailable, it falls back to splitting
  combined KinoPub titles in the `"Localized / Original"` format.
- 

## Trakt Importer Mapping (PyTrakt)

Implemented in `traktv-importer.py`.

Scope:
- Imports `data/history.json` to Trakt `sync/history`.
- Imports `data/watchlist.json` to Trakt `sync/watchlist`.
- Derives dropped-show candidates from `data/history.json` shows minus active `data/watching.json` `watching[]` and sends them to Trakt hidden dropped (`users/hidden/dropped`).
- Authentication for live import uses cached tokens with refresh and falls back to Trakt OAuth Device Code Flow.

Field mapping:
- `history[].imdb_id` -> `ids.imdb`.
- `history[].watched_at` -> `watched_at` (UTC ISO-8601 with `Z`).
- `history[].type == "movie"` -> Trakt movie history entries.
- `history[].type == "show"` with `season` and `episode` -> Trakt show/season/episode history entries.
- `watchlist.movies[]` and `watchlist.shows[]` -> Trakt watchlist movie/show entries.
- History show IMDb IDs not present in `watching.watching[]` (or TMDB-resolved IMDb) -> Trakt `users/hidden/dropped` show entries.

Identifier policy:
- Primary identifier is IMDb ID.
- If IMDb is missing/invalid, importer attempts TMDB title/year lookup (when `TMDB_API_KEY` is provided) and then uses external IMDb ID.
- Items unresolved to IMDb are skipped with warnings.

3D compatibility:
- KinoPub `is_3d` is treated as local metadata only.
- Importer sends 3D watches to Trakt as standard movie history entries.

Resilience and idempotency:
- Importer deduplicates outgoing payloads before sync.
- Retry with backoff is used for transient failures.
- Rate-limit (429) is respected via retry-after handling.
- Sync summary is written to `data/trakt_sync_state.json`.

Device flow behavior:
- Script first tries cached Trakt token from `data/trakt_token_cache.json` (or `--token-cache-file`).
- If access token is expired, script attempts refresh via `/oauth/token`.
- If cache/refresh cannot be used, script requests a device code from `/oauth/device/code` and polls `/oauth/device/token`.
