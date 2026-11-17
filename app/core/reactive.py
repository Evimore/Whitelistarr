"""Reactive poller: fast, cheap reactions between full sweeps.

Two jobs run every ``REACTIVE_INTERVAL_SECONDS`` (default 60), so the app reacts
to changes in seconds without a manual Plex webhook (Plex Pass-only) and without
waiting for the hourly reconcile sweep:

- **Arr tag-change diff** — pull the Radarr/Sonarr guid->tags index (arr APIs
  only, never iterates Plex), diff it against the previous snapshot, and
  targeted-reconcile only the Plex items whose tags actually changed. Radarr and
  Sonarr emit no "tag changed" webhook, so diffing is the only way to react.
- **Plex recently-added** — ask Plex for items added since a high-watermark and
  label them, replacing the optional Plex ``library.new`` webhook.

State is in-memory: on the first poll each path *baselines* (records the current
state, reacts to nothing) because the sweep owns the cold start. A restart simply
re-baselines; the sweep remains the safety net. Reconcile is idempotent, so the
poller never fights the sweep.

A change that fails to apply is retried on the next poll rather than dropped.
If Plex (or an *arr) can't be reached, the poll stops and keeps every change it
hadn't applied yet. An item that keeps failing for a reason of its own is
retried ``MAX_ATTEMPTS`` times, then left to the sweep.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
import requests

from app.core.matching import parse_guid

log = logging.getLogger(__name__)

# Bounds a cold catch-up of recently-added items in a single poll.
RECENT_CAP = 100

# Polls a failing change is retried on before it's left to the sweep. Attempts
# made while a service is unreachable don't count.
MAX_ATTEMPTS = 5


def _unreachable(exc: BaseException) -> bool:
    """Whether a failure means Plex or an *arr can't be reached at all right now."""
    from app.clients.plex import PlexUnavailableError

    return isinstance(
        exc,
        (
            PlexUnavailableError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            httpx.TransportError,
        ),
    )


def _use_attempt(attempts: dict[str, int], key: str) -> bool:
    """Count a failed attempt at ``key``. True while it has attempts left."""
    attempts[key] = attempts.get(key, 0) + 1
    if attempts[key] < MAX_ATTEMPTS:
        return True
    del attempts[key]
    return False


class ReactivePoller:
    def __init__(
        self,
        plex: Any,
        label_sync: Any,
        radarr: Any,
        sonarr: Any,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._plex = plex
        self._sync = label_sync
        self._radarr = radarr
        self._sonarr = sonarr
        self._now = now_fn or datetime.now
        # guid_key -> frozenset(tag names) at the previous poll. None = not yet baselined.
        self._prev: dict[str, frozenset[str]] | None = None
        # Newest Plex addedAt we've reacted to. None = not yet baselined.
        self._watermark: datetime | None = None
        # Failed attempts so far: per guid_key for tag changes, and per Plex
        # ratingKey for recently-added items, which are refetched to retry.
        self._tag_attempts: dict[str, int] = {}
        self._added_retries: dict[str, int] = {}

    def poll(self) -> dict[str, Any]:
        merged, types, snapshot = self._build_arr_index()
        tag_changes = self._react_to_tag_changes(merged, types, snapshot)
        added_titles = self._react_to_recently_added(merged)
        summary = {
            "tag_changes": tag_changes,
            "added": len(added_titles),
            "added_titles": added_titles,
        }
        if tag_changes or added_titles:
            log.info(
                "Reactive poll: tag_changes=%d added=%d", tag_changes, len(added_titles)
            )
        return summary

    # --- arr tag-change diff ------------------------------------------------

    def _build_arr_index(
        self,
    ) -> tuple[dict[str, set[str]], dict[str, str], dict[str, frozenset[str]]]:
        """Return (merged guid->tags, guid->media_type, snapshot guid->frozenset tags).

        Unlike ``LabelSync.build_index`` this keeps untagged items, so a tag being
        *added* to a previously-untagged item shows up as a snapshot change.
        """
        merged: dict[str, set[str]] = {}
        types: dict[str, str] = {}
        snapshot: dict[str, frozenset[str]] = {}
        for client, media_type in ((self._radarr, "movie"), (self._sonarr, "show")):
            for keys, tags in client.iter_all_with_tags():
                tagset = frozenset(tags)
                for key in keys:
                    if tags:
                        merged.setdefault(key, set()).update(tags)
                    types.setdefault(key, media_type)
                    snapshot[key] = snapshot.get(key, frozenset()) | tagset
        return merged, types, snapshot

    def _react_to_tag_changes(
        self,
        merged: dict[str, set[str]],
        types: dict[str, str],
        snapshot: dict[str, frozenset[str]],
    ) -> int:
        if self._prev is None:  # baseline: react to nothing
            self._prev = snapshot
            return 0
        prev = self._prev
        changed_keys = [key for key, tags in snapshot.items() if tags != prev.get(key, frozenset())]

        retry: set[str] = set()  # changes to detect again on the next poll
        reconciled: set[str] = set()  # rating_keys, to avoid double work per item
        count = 0
        for i, key in enumerate(changed_keys):
            try:
                item = self._resolve(key, types.get(key))
                if item is not None:
                    rk = getattr(item, "rating_key", None) or item.title
                    if rk not in reconciled:
                        self._sync.reconcile_found(item, merged)
                        reconciled.add(rk)
                        count += 1
            except Exception as exc:  # noqa: BLE001 - one bad item shouldn't abort the poll
                if _unreachable(exc):
                    pending = changed_keys[i:]
                    log.warning(
                        "Reactive poll stopped, retrying %d tag change(s) next poll: %s",
                        len(pending),
                        exc,
                    )
                    retry.update(pending)
                    break
                if _use_attempt(self._tag_attempts, key):
                    log.exception("Reactive reconcile failed for %s; retrying next poll", key)
                    retry.add(key)
                else:
                    log.exception("Reactive reconcile failed for %s; leaving it to the sweep", key)
                continue
            self._tag_attempts.pop(key, None)

        # Hold a change that wasn't applied at its old tags, so it's seen again.
        for key in retry:
            if key in prev:
                snapshot[key] = prev[key]
            else:
                snapshot.pop(key, None)
        self._prev = snapshot
        return count

    def _resolve(self, guid_key: str, media_type: str | None) -> Any:
        """Find the Plex item for a changed guid_key (e.g. ``tmdb:603``)."""
        if media_type is None:
            return None
        parsed = parse_guid(guid_key.replace(":", "://", 1))
        if parsed is None:
            return None
        source, value = parsed
        try:
            identifier = int(value)
        except (TypeError, ValueError):
            return None
        # Resolve through LabelSync so legacy Plex items indexed under a
        # different id (e.g. imdb-only) still match via the *arr id bridge.
        if source == "tmdb":
            return self._sync.resolve_plex_item(media_type, tmdb_id=identifier)
        if source == "tvdb":
            return self._sync.resolve_plex_item(media_type, tvdb_id=identifier)
        return None  # imdb-only: same item carries a tmdb/tvdb key handled above

    # --- Plex recently-added ------------------------------------------------

    def _react_to_recently_added(self, merged: dict[str, set[str]]) -> list[str]:
        if self._watermark is None:  # baseline: only react to items added from now on
            self._watermark = self._now()
            return []
        recent = self._plex.recently_added(self._watermark, cap=RECENT_CAP)
        # The watermark moves past every item returned, failed ones included: an
        # item that fails is retried by refetching it by ratingKey instead.
        for item in recent:
            added_at = getattr(item, "added_at", None)
            if added_at is not None and added_at > self._watermark:
                self._watermark = added_at

        # (ratingKey, item) pairs; item None = refetch it (a retry from a past poll).
        work: list[tuple[str | None, Any]] = [
            (getattr(item, "rating_key", None), item) for item in recent
        ]
        queued = {rk for rk, _ in work}
        work += [(rk, None) for rk in self._added_retries if rk not in queued]

        titles: list[str] = []
        for i, (rk, item) in enumerate(work):
            try:
                if item is None:
                    item = self._plex.fetch_labelable(rk)
                    if item is None:  # no longer in Plex
                        self._added_retries.pop(rk, None)
                        continue
                self._sync.reconcile_found(item, merged)
            except Exception as exc:  # noqa: BLE001 - one bad item shouldn't abort the poll
                if _unreachable(exc):
                    pending = [prk for prk, _ in work[i:] if prk is not None]
                    log.warning(
                        "Reactive poll stopped, retrying %d recently-added item(s) next poll: %s",
                        len(pending),
                        exc,
                    )
                    for prk in pending:
                        self._added_retries.setdefault(prk, 0)
                    break
                name = getattr(item, "title", rk)
                if rk is not None and _use_attempt(self._added_retries, rk):
                    log.exception("Reactive reconcile failed for %s; retrying next poll", name)
                else:
                    log.exception("Reactive reconcile failed for %s; leaving it to the sweep", name)
                continue
            self._added_retries.pop(rk, None)
            titles.append(item.title)
        return titles
