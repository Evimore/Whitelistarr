from datetime import datetime

from app.clients.notify import Notifier
from app.clients.seerr import RequestInfo
from app.core.watch_monitor import (
    WatchMonitor,
    is_stale,
    last_watched_at,
    max_watched_percent,
)
from app.state import StateStore

NOW = datetime(2026, 6, 14, 12, 0, 0)


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


class TestPureHelpers:
    def test_max_watched_percent(self):
        rows = [{"percent_complete": 40}, {"percent_complete": 92}, {"percent_complete": 10}]
        assert max_watched_percent(rows) == 92

    def test_max_watched_percent_empty(self):
        assert max_watched_percent([]) == 0

    def test_last_watched_at_picks_latest(self):
        old = _epoch(datetime(2026, 1, 1))
        recent = _epoch(datetime(2026, 6, 1))
        rows = [{"date": old}, {"date": recent}]
        assert last_watched_at(rows) == datetime.fromtimestamp(recent)

    def test_last_watched_at_none_when_empty(self):
        assert last_watched_at([]) is None

    def test_is_stale_true_when_old_and_never_watched(self):
        added = datetime(2025, 1, 1)  # >180 days before NOW
        assert is_stale(added, NOW, 180, None, 90) is True

    def test_is_stale_false_when_recently_added(self):
        added = datetime(2026, 6, 1)  # <180 days
        assert is_stale(added, NOW, 180, None, 90) is False

    def test_is_stale_false_when_watched_recently(self):
        added = datetime(2025, 1, 1)
        last = datetime(2026, 6, 1)  # within 90 days of NOW
        assert is_stale(added, NOW, 180, last, 90) is False

    def test_is_stale_true_when_watched_long_ago(self):
        added = datetime(2025, 1, 1)
        last = datetime(2025, 2, 1)  # >90 days before NOW
        assert is_stale(added, NOW, 180, last, 90) is True


class FakePlexItem:
    def __init__(self, title, rating_key, added_at):
        self.title = title
        self.rating_key = rating_key
        self.added_at = added_at
        self.media_type = "movie"
        self.tmdb_id = 603
        self.tvdb_id = None


class FakePlex:
    def __init__(self, item):
        self._item = item

    def find_item(self, media_type, tmdb_id=None, tvdb_id=None):
        return self._item


class FakeTautulli:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def get_history(self, rating_key=None, user=None, length=100, grandparent_rating_key=None):
        self.calls.append({"rating_key": rating_key, "grandparent_rating_key": grandparent_rating_key})
        return self._rows


class FakeNotifier:
    def __init__(self, results=()):
        self.sent = []
        self._results = list(results)  # per-call return values; True once exhausted

    def notify(self, title, body):
        self.sent.append((title, body))
        return self._results.pop(0) if self._results else True


def _request():
    return RequestInfo(
        media_type="movie", tmdb_id=603, tvdb_id=None,
        requester="family", requester_name="Family", rating_key=None,
    )


def _tv_request():
    return RequestInfo(
        media_type="tv", tmdb_id=None, tvdb_id=353546,
        requester="family", requester_name="Family", rating_key=None,
    )


def _monitor(
    plex, tautulli, notifier, state, events=("watched", "stale"), resolve_item=None,
    dry_run=False, request=None,
):
    req = request or _request()
    return WatchMonitor(
        overseerr=type("O", (), {"iter_requests": lambda self: [req]})(),
        tautulli=tautulli,
        plex=plex,
        notifier=notifier,
        state=state,
        events=list(events),
        watched_percent=85,
        stale_after_days=180,
        unwatched_after_days=90,
        now_fn=lambda: NOW,
        resolve_item=resolve_item,
        dry_run=dry_run,
    )


def test_notifies_when_requester_watched():
    item = FakePlexItem("Dune", "111", added_at=datetime(2026, 6, 1))
    plex = FakePlex(item)
    tautulli = FakeTautulli([{"percent_complete": 95, "date": _epoch(datetime(2026, 6, 10))}])
    notifier = FakeNotifier()
    state = StateStore(":memory:")
    _monitor(plex, tautulli, notifier, state).scan()
    assert len(notifier.sent) == 1
    assert "Family" in notifier.sent[0][1]
    assert "Dune" in notifier.sent[0][1]


def test_watched_notification_deduped():
    item = FakePlexItem("Dune", "111", added_at=datetime(2026, 6, 1))
    plex = FakePlex(item)
    tautulli = FakeTautulli([{"percent_complete": 95, "date": _epoch(datetime(2026, 6, 10))}])
    notifier = FakeNotifier()
    state = StateStore(":memory:")
    mon = _monitor(plex, tautulli, notifier, state)
    mon.scan()
    mon.scan()
    assert len(notifier.sent) == 1


def test_failed_delivery_is_retried_on_next_scan():
    # Apprise returns False (it doesn't raise) when a send fails, e.g. Discord is down.
    item = FakePlexItem("Dune", "111", added_at=datetime(2026, 6, 1))
    tautulli = FakeTautulli([{"percent_complete": 95, "date": _epoch(datetime(2026, 6, 10))}])
    notifier = FakeNotifier(results=[False])
    mon = _monitor(FakePlex(item), tautulli, notifier, StateStore(":memory:"), events=("watched",))

    assert mon.scan()["notified"] == 0
    assert mon.scan()["notified"] == 1
    assert mon.scan()["notified"] == 0  # delivered once -> deduped from then on
    assert len(notifier.sent) == 2


class FakeApprise:
    def __init__(self):
        self.sent = []

    def __len__(self):
        return 1

    def notify(self, title, body, **kwargs):
        self.sent.append(title)
        return True


def test_dry_run_does_not_use_up_the_notification():
    # The README suggests a DRY_RUN first run. Events seen during it must still
    # notify once DRY_RUN is turned off.
    item = FakePlexItem("Dune", "111", added_at=datetime(2026, 6, 1))
    tautulli = FakeTautulli([{"percent_complete": 95, "date": _epoch(datetime(2026, 6, 10))}])
    state = StateStore(":memory:")
    dry_apprise, live_apprise = FakeApprise(), FakeApprise()

    dry = Notifier([], dry_run=True, apprise_obj=dry_apprise)
    summary = _monitor(FakePlex(item), tautulli, dry, state, events=("watched",), dry_run=True).scan()
    assert summary["notified"] == 1  # reported as "would notify"
    assert dry_apprise.sent == []    # but nothing actually sent

    live = Notifier([], apprise_obj=live_apprise)
    _monitor(FakePlex(item), tautulli, live, state, events=("watched",)).scan()
    assert live_apprise.sent == ["Requester finished watching"]


def test_notifies_stale_when_old_and_unwatched():
    item = FakePlexItem("Old Movie", "222", added_at=datetime(2025, 1, 1))
    plex = FakePlex(item)
    tautulli = FakeTautulli([])  # never watched
    notifier = FakeNotifier()
    state = StateStore(":memory:")
    _monitor(plex, tautulli, notifier, state, events=("stale",)).scan()
    assert len(notifier.sent) == 1
    assert "Old Movie" in notifier.sent[0][1]


def test_no_notification_when_not_watched_and_not_stale():
    item = FakePlexItem("Fresh", "333", added_at=datetime(2026, 6, 1))
    plex = FakePlex(item)
    tautulli = FakeTautulli([{"percent_complete": 10}])
    notifier = FakeNotifier()
    state = StateStore(":memory:")
    _monitor(plex, tautulli, notifier, state).scan()
    assert notifier.sent == []


def test_injected_resolver_is_used_instead_of_plex_find_item():
    # When wired to LabelSync.resolve_plex_item (the *arr id bridge), the monitor
    # must resolve through it, not plex.find_item — that's how legacy libraries
    # get their watch/stale lookups resolved.
    item = FakePlexItem("Dune", "111", added_at=datetime(2026, 6, 1))
    calls = []

    def resolver(media_type, tmdb_id=None, tvdb_id=None):
        calls.append((media_type, tmdb_id, tvdb_id))
        return item

    class ExplodingPlex:
        def find_item(self, *a, **k):  # must not be called
            raise AssertionError("plex.find_item should not be used when resolver is injected")

    tautulli = FakeTautulli([{"percent_complete": 95, "date": _epoch(datetime(2026, 6, 10))}])
    notifier = FakeNotifier()
    _monitor(ExplodingPlex(), tautulli, notifier, StateStore(":memory:"), resolve_item=resolver).scan()
    assert calls == [("movie", 603, None)]
    assert len(notifier.sent) == 1


# --- TV requests ------------------------------------------------------------

def _show_resolver(item, calls):
    def resolver(media_type, tmdb_id=None, tvdb_id=None):
        calls.append(media_type)
        return item if media_type == "show" else None  # Plex sections are "movie"/"show"

    return resolver


def test_tv_request_resolves_as_a_plex_show():
    # Seerr calls TV "tv"; Plex sections are "show". Passing "tv" through never matched.
    item = FakePlexItem("Bluey", "500", added_at=datetime(2025, 1, 1))
    calls = []
    mon = _monitor(None, FakeTautulli([]), FakeNotifier(), StateStore(":memory:"),
                   events=("stale",), resolve_item=_show_resolver(item, calls),
                   request=_tv_request())
    assert mon.scan()["notified"] == 1
    assert calls == ["show"]


def test_tv_history_is_looked_up_by_the_show_as_grandparent():
    item = FakePlexItem("Bluey", "500", added_at=datetime(2025, 1, 1))
    tautulli = FakeTautulli([])
    _monitor(None, tautulli, FakeNotifier(), StateStore(":memory:"), events=("stale",),
             resolve_item=_show_resolver(item, []), request=_tv_request()).scan()
    assert tautulli.calls == [{"rating_key": None, "grandparent_rating_key": "500"}]


def test_tv_show_watched_recently_is_not_stale():
    item = FakePlexItem("Bluey", "500", added_at=datetime(2025, 1, 1))
    episode_play = {"percent_complete": 100, "date": _epoch(datetime(2026, 6, 1))}
    notifier = FakeNotifier()
    _monitor(None, FakeTautulli([episode_play]), notifier, StateStore(":memory:"),
             events=("stale",), resolve_item=_show_resolver(item, []),
             request=_tv_request()).scan()
    assert notifier.sent == []


def test_watched_event_is_movies_only():
    # One episode past the threshold doesn't mean the requester finished the show.
    item = FakePlexItem("Bluey", "500", added_at=datetime(2026, 6, 1))
    episode_play = {"percent_complete": 100, "date": _epoch(datetime(2026, 6, 10))}
    notifier = FakeNotifier()
    _monitor(None, FakeTautulli([episode_play]), notifier, StateStore(":memory:"),
             events=("watched",), resolve_item=_show_resolver(item, []),
             request=_tv_request()).scan()
    assert notifier.sent == []


def test_movie_history_is_still_looked_up_by_rating_key():
    item = FakePlexItem("Dune", "111", added_at=datetime(2026, 6, 1))
    tautulli = FakeTautulli([])
    _monitor(FakePlex(item), tautulli, FakeNotifier(), StateStore(":memory:")).scan()
    assert tautulli.calls == [{"rating_key": "111", "grandparent_rating_key": None}]
