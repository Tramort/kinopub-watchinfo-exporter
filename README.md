# KinoPub watch info exporter

Set of scripts to export KinoPub watch information and export to other services.

Features:
- exporting watch history, watchlist, and favorites from KinoPub to a file.
- watchlist export includes movies and shows.
- support KinoPub type 3d export in history with `is_3d` flag; watchlist includes 3D titles under `movies`.
- (Planned) export to Trakt.tv (import watch information from KinoPub to Trakt.tv).
- Trakt.tv note: Trakt does not support 3D as a separate item type; 3D entries should be mapped to standard movie items.
- (Maybe) support playback and movies for watchlist.

kinopub-exporter: export history, watchlist (currently watching series), favorites (to watch in future) from KinoPub to file.
(Planned) traktv-importer: import KinoPub watch information to Trakt.tv.
