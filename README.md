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

## KinoPub Exporter

Run:
- `python kinopub-exporter.py`

Useful flags:
- `--full`
- `--since 2026-07-05T12:00:00Z`
- `--history-file data/history.json`
- `--base-url https://api.service-kp.com`
- `--raw-dump` (writes one raw successful API JSON response per file into `data/kinopub_raw_dumps/`)
- `--raw-dump data/my_kinopub_raw_dumps` (custom dump directory)

traktv-importer: import `data/history.json` and `data/watchlist.json` to Trakt.tv using PyTrakt, and remove dropped shows from Trakt watchlist using `data/watching.json`.

## Trakt Importer

Requirements:
- Trakt API app client id (`TRAKT_CLIENT_ID`)
- Trakt API app client secret (`TRAKT_CLIENT_SECRET`)
- Optional TMDB API key (`TMDB_API_KEY`) for resolving missing IMDb IDs by title/year (also tries `year±1`).

Run:
- `python traktv-importer.py --dry-run`
- `python traktv-importer.py`

Useful flags:
- `--history-file data/history.json`
- `--watchlist-file data/watchlist.json`
- `--watching-file data/watching.json`
- `--state-file data/trakt_sync_state.json`
- `--token-cache-file data/trakt_token_cache.json`
- `--mismatch-mode off|approve` (default: `approve`)
- `--mismatch-approve-cache-file data/trakt_mismatch_approvals.json`
- `--mismatch-approve-cache-clean` (clear approval cache before run)
- `--mismatch-max-gap 1`
- `--device-auth-timeout 900`
- `--batch-size 100`
- `--retry-attempts 3`
- `--rate-limit-delay 1.1`

Notes:
- This importer syncs history and watchlist, and removes dropped shows from Trakt watchlist.
- Dropped-show candidates are taken directly from `watching.json` `dropped[]` entries.
- `watching.json` `watching[]` entries with `progress.is_finished == true` are also marked as watched in Trakt history.
- In `--mismatch-mode approve`, the importer creates proposals for:
	- inferred tail episodes when Trakt season has up to `--mismatch-max-gap` more episodes than KinoPub progress for any season `1..last_watched_season` (earlier seasons use `history.json`, final season uses watching progress)
	- hiding finished shows as dropped when KinoPub and Trakt season counts differ
	- dropping finished shows when no inference candidate exists
- If KinoPub last watched episode equals or exceeds Trakt season max episode, no mismatch proposal is created.
- Before each proposal, importer logs a human-readable mismatch explanation.
- For unresolved proposals, importer asks an interactive question with options `approve|reject|defer`; default is `approve`.
- Proposals are persisted in `data/trakt_mismatch_approvals.json` with exact fingerprint decisions.
- If any proposal remains unresolved (`defer` or no interactive input), importer aborts before sending any data to Trakt.
- KinoPub `is_3d` metadata is imported as standard Trakt movie history items.
- For live imports, the script uses cached Trakt tokens when possible and falls back to Device Code Flow when needed.
