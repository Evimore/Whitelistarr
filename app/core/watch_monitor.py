"""Watch-milestone and stale-content detection -> Apprise notifications.

Pure helpers decide watched/stale; ``WatchMonitor.scan`` walks Overseerr requests,
cross-references Tautulli history, and emits deduped notifications. No state is
written back to Radarr/Sonarr (unlike the reference tool) -- notifications only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

# Seerr media type -> the Plex-side media type the resolver matches sections on.
_PLEX_MEDIA_TYPE = {"movie": "movie", "tv": "show"}


def max_watched_percent(rows: list[dict[str, Any]]) -> int:
    return max((int(r.get("percent_complete") or 0) for r in rows), default=0)


def last_watched_at(rows: list[dict[str, Any]]) -> datetime | None:
    epochs = [int(r["date"]) for r in rows if r.get("date")]
    if not epochs:
        return None
    return datetime.fromtimestamp(max(epochs))


def is_stale(
    added_at: datetime | None,
    now: datetime,
    stale_after_days: int,
    last_watched: datetime | None,
    unwatched_after_days: int,
) -> bool:
    if added_at is None:
        return False
    if (now - added_at).days < stale_after_days:
        return False
    if last_watched is None:
        return True
    return (now - last_watched).days >= unwatched_after_days


class WatchMonitor:
    def __init__(
        self,
        overseerr: Any,
        tautulli: Any,
        plex: Any,
        notifier: Any,
        state: Any,
        *,
        events: list[str],
        watched_percent: int,
        stale_after_days: int,
        unwatched_after_days: int,
        now_fn: Callable[[], datetime] = datetime.now,
        resolve_item: Callable[..., Any] | None = None,
        dry_run: bool = False,
    ) -> None:
        self._overseerr = overseerr
        self._tautulli = tautulli
        self._plex = plex
        self._notifier = notifier
        self._state = state
        self._events = events
        self._watched_percent = watched_percent
        self._stale_after_days = stale_after_days
        self._unwatched_after_days = unwatched_after_days
        self._now_fn = now_fn
        # Resolver for id -> Plex item. Defaults to plex.find_item; wired to
        # LabelSync.resolve_plex_item so legacy-agent libraries resolve via the
        # *arr id bridge (Overseerr gives tmdb/tvdb; a legacy item may be indexed
        # under imdb only).
        self._resolve_item = resolve_item or plex.find_item
        self._dry_run = dry_run

    def scan(self) -> dict[str, int]:
        processed = 0
        notified = 0
        for req in self._overseerr.iter_requests():
            processed += 1
            try:
                notified += self._process(req)
            except Exception:  # noqa: BLE001 - one bad request shouldn't abort the scan
                log.exception("watch scan failed for tmdb=%s tvdb=%s", req.tmdb_id, req.tvdb_id)
        log.info("Watch scan complete: processed=%d notified=%d", processed, notified)
        return {"processed": processed, "notified": notified}

    def _process(self, req: Any) -> int:
        media_type = _PLEX_MEDIA_TYPE.get(req.media_type)
        if media_type is None:
            return 0
        item = self._resolve_item(media_type, tmdb_id=req.tmdb_id, tvdb_id=req.tvdb_id)
        if item is None:
            return 0
        is_show = media_type == "show"
        if is_show:
            # Tautulli's rating_key matches the played item, and a show's plays
            # are its episodes, which carry the show's key as their grandparent.
            rows = self._tautulli.get_history(
                grandparent_rating_key=item.rating_key, user=req.requester
            )
        else:
            rows = self._tautulli.get_history(rating_key=item.rating_key, user=req.requester)
        who = req.requester_name or req.requester or "Someone"
        count = 0

        # Movies only: one episode past the threshold isn't "finished watching" a show.
        if "watched" in self._events and not is_show:
            if max_watched_percent(rows) >= self._watched_percent:
                count += self._notify_once(
                    f"{item.rating_key}:watched",
                    "Requester finished watching",
                    f"{who} finished watching {item.title}",
                )

        if "stale" in self._events:
            stale = is_stale(
                item.added_at,
                self._now_fn(),
                self._stale_after_days,
                last_watched_at(rows),
                self._unwatched_after_days,
            )
            if stale:
                count += self._notify_once(
                    f"{item.rating_key}:stale",
                    "Stale request",
                    f"{item.title} (requested by {who}) is still unwatched",
                )

        return count

    def _notify_once(self, key: str, title: str, body: str) -> int:
        """Send a notification unless ``key`` was already sent. Returns 1 if sent.

        The key is recorded only after a real, successful send, so the event is
        retried on the next scan otherwise. Apprise reports a failed delivery by
        returning False, not by raising. In DRY_RUN the notifier only logs, and
        nothing is recorded, so the event still notifies once DRY_RUN is off.
        """
        if self._state.already_notified(key):
            return 0
        if not self._notifier.notify(title, body):
            log.warning("Notification %r for %s was not delivered; retrying next scan", title, key)
            return 0
        if not self._dry_run:
            self._state.mark_notified(key)
        return 1
