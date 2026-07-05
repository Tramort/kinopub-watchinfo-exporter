# Kinopub-watchinfo-exporter

Goals: export history, watchlist(currently watching series), favorites (to watch in future) from kinopub.

API doc: https://kinoapi.com
Note: API may be outdated; implement version detection and fallback logic

Supported types: movie, series (with episode/season granularity)

## Requirements:
### Output examples
Watching history:
```
{
  "history": [
    {
      "title": "Черное зеркало",
      "imdb_id": "tt2085059",
      "kinopoisk_id": 655800,
      "type": "show",
      "season": 1,
      "episode": 1,
      "watched_at": "2026-05-10T21:00:00Z"
    },
    {
      "title": "Интерстеллар",
      "imdb_id": "tt0816692",
      "kinopoisk_id": 258687,
      "type": "movie",
      "season": null,
      "episode": null,
      "watched_at": "2026-05-12T18:30:00Z"
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
        "imdb_id": "tt1375666",
        "kinopoisk_id": 447301,
        "added_at": "2026-07-05T12:00:00Z"
      }
    ],
    "shows": [
      {
        "title": "Во все тяжкие",
        "original_title": "Breaking Bad",
        "imdb_id": "tt0903747",
        "kinopoisk_id": 404900,
        "added_at": "2026-07-05T12:05:00Z"
      }
    ]
  }
}
```
Currently Watching(only series):
```
{
  "currently_watching": [
    {
      "title": "Пацаны",
      "original_title": "The Boys",
      "imdb_id": "tt1190634",
      "kinopoisk_id": 1113943,
      "progress": {
        "last_watched_season": 4,
        "last_watched_episode": 8,
        "is_finished": false
      },
      "last_viewed_at": "2026-07-01T20:30:00Z"
    }
  ]
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
  - Validate IMDb IDs before export
- The is_finished Marker: Tracking whether a show is fully completed helps platforms like Simkl automatically archive
  it, preventing your active "Watching" list from getting cluttered with dead weight.
- Multi-part Movie DuplicatesMovies split into multiple video files on Kino.pub register as multiple full views in your
  history log. They require deduplication within a 24-hour window.
- Bonus Material Pollution: Clicking on short clips, trailers, or "making-of" videos registers as a full movie watch
  event. These must be filtered out by keywords or duration thresholds.
- Season 0 (Specials) Mismatch: Special episode numbering on Kino.pub rarely matches official databases (TMDB/TVDB).
  It is safest to skip season == 0 entirely to avoid messed-up history syncs.
