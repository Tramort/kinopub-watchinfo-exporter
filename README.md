# KinoPub watch info exporter

Set of scripts to export KinoPub watch information and export to other services.

Features:
- exporting watch history, watchlist, and favorites from KinoPub to a file.
- watchlist export includes movies and shows.
- support KinoPub type 3d export in history with `is_3d` flag; watchlist includes 3D titles under `movies`.
- export to Trakt.tv (import watch information from KinoPub to Trakt.tv).
- Trakt.tv note: Trakt does not support 3D as a separate item type; 3D entries should be mapped to standard movie items.
- (Maybe) support playback and movies for watchlist.

kinopub-exporter: export history, watchlist, and watching series data from KinoPub to file.
traktv-importer: import KinoPub watch information to Trakt.tv.

traktv-importer: import `data/history.json` and `data/watchlist.json` to Trakt.tv using PyTrakt, and remove dropped shows from Trakt watchlist using `data/watching.json`.

## Trakt Importer

Requirements:
- Trakt API app client id (`TRAKT_CLIENT_ID`)
- Trakt API app client secret (`TRAKT_CLIENT_SECRET`)
- Optional TMDB API key (`TMDB_API_KEY`) for resolving missing IMDb IDs by title/year.

Run:
- `python traktv-importer.py --dry-run`
- `python traktv-importer.py`

Useful flags:
- `--history-file data/history.json`
- `--watchlist-file data/watchlist.json`
- `--watching-file data/watching.json`
- `--state-file data/trakt_sync_state.json`
- `--token-cache-file data/trakt_token_cache.json`
- `--device-auth-timeout 900`
- `--batch-size 100`
- `--retry-attempts 3`
- `--rate-limit-delay 1.1`

Notes:
- This importer syncs history and watchlist, and removes dropped shows from Trakt watchlist.
- Dropped-show candidates are derived from `history.json` show entries minus active `watching.json` `watching[]` entries.
- KinoPub `is_3d` metadata is imported as standard Trakt movie history items.
- For live imports, the script uses cached Trakt tokens when possible and falls back to Device Code Flow when needed.
