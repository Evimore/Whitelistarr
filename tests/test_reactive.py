from datetime import datetime, timedelta

from app.core.reactive import ReactivePoller
from app.core.sync import LabelSync

LABEL_MAP = {"kids": "kids-allowed", "family": "shared"}
MANAGED = set(LABEL_MAP.values())


class FakePlexItem:
    def __init__(self, title, media_type, labels, guid_keys, rating_key=None, added_at=None):
        self.title = title
        self.media_type = media_type
        self.rating_key = rating_key
        self.added_at = added_at
        self._labels = set(labels)
        self._guid_keys = set(guid_keys)

    def guid_keys(self):
        return set(self._guid_keys)

    def labels(self):
        return set(self._labels)

    def add_labels(self, labels):
        self._labels.update(labels)

    def remove_labels(self, labels):
        for label in labels:
            self._labels.discard(label)


class FakePlex:
    def __init__(self, items=None, recent=None):
        self._items = items or []
        self._recent = recent if recent is not None else []

    def iter_items(self):
        return list(self._items)

    def find_item_by_keys(self, media_type, keys):
        keys = set(keys)
        for item in self._items:
            if item.media_type == media_type and (item.guid_keys() & keys):
                return item
        return None

    def find_item(self, media_type, tmdb_id=None, tvdb_id=None):
        targets = set()
        if tmdb_id is not None:
            targets.add(f"tmdb:{tmdb_id}")
        if tvdb_id is not None:
            targets.add(f"tvdb:{tvdb_id}")
        return self.find_item_by_keys(media_type, targets)

    def recently_added(self, since, cap=100):
        out = [
            it for it in self._recent
            if since is None or (it.added_at is not None and it.added_at > since)
        ]
        out.sort(key=lambda it: it.added_at, reverse=True)
        return out[:cap]


class FakeArr:
    def __init__(self, entries=None):
        self._entries = entries or []  # list of (guid_keys, tag_names)

    def set(self, entries):
        self._entries = entries

    def iter_all_with_tags(self):
        return list(self._entries)


def make_poller(plex, radarr=None, sonarr=None, now=None):
    radarr = radarr or FakeArr()
    sonarr = sonarr or FakeArr()
    ls = LabelSync(
        plex=plex, radarr=radarr, sonarr=sonarr,
        label_map=LABEL_MAP, managed=MANAGED, mode="reconcile",
    )
    now_fn = (lambda: now) if now is not None else None
    return ReactivePoller(plex=plex, label_sync=ls, radarr=radarr, sonarr=sonarr, now_fn=now_fn)


# --- arr tag-change diffing -------------------------------------------------

def test_first_poll_baselines_arr_and_changes_nothing():
    item = FakePlexItem("Dune", "movie", set(), {"tmdb:603"}, rating_key="1")
    radarr = FakeArr([({"tmdb:603"}, ["kids"])])
    poller = make_poller(FakePlex([item]), radarr=radarr, now=datetime(2026, 1, 1))
    summary = poller.poll()
    assert item.labels() == set()           # baseline does not label
    assert summary["tag_changes"] == 0


def test_tag_added_to_existing_item_reacts_on_next_poll():
    item = FakePlexItem("Dune", "movie", set(), {"tmdb:603"}, rating_key="1")
    radarr = FakeArr([({"tmdb:603"}, [])])  # untagged at baseline
    poller = make_poller(FakePlex([item]), radarr=radarr, now=datetime(2026, 1, 1))
    poller.poll()                            # baseline
    radarr.set([({"tmdb:603"}, ["kids"])])   # tag added in Radarr
    summary = poller.poll()
    assert item.labels() == {"kids-allowed"}
    assert summary["tag_changes"] >= 1


def test_tag_removed_reconciles_removal():
    item = FakePlexItem("Dune", "movie", {"kids-allowed"}, {"tmdb:603"}, rating_key="1")
    radarr = FakeArr([({"tmdb:603"}, ["kids"])])
    poller = make_poller(FakePlex([item]), radarr=radarr, now=datetime(2026, 1, 1))
    poller.poll()                            # baseline
    radarr.set([({"tmdb:603"}, [])])         # tag removed
    poller.poll()
    assert item.labels() == set()


def test_no_arr_change_means_no_reaction():
    item = FakePlexItem("Dune", "movie", set(), {"tmdb:603"}, rating_key="1")
    radarr = FakeArr([({"tmdb:603"}, ["kids"])])
    poller = make_poller(FakePlex([item]), radarr=radarr, now=datetime(2026, 1, 1))
    poller.poll()
    summary = poller.poll()
    assert summary["tag_changes"] == 0
    # baseline never labeled it, and nothing changed since
    assert item.labels() == set()


def test_tag_change_bridges_to_imdb_only_legacy_item():
    # Legacy movie indexed under imdb only; Radarr key is tmdb. The tmdb tag
    # change must still resolve the Plex item via the imdb sibling id.
    item = FakePlexItem("Train Dreams", "movie", set(), {"imdb:tt1"}, rating_key="1")
    radarr = FakeArr([({"tmdb:99", "imdb:tt1"}, [])])
    poller = make_poller(FakePlex([item]), radarr=radarr, now=datetime(2026, 1, 1))
    poller.poll()                                    # baseline
    radarr.set([({"tmdb:99", "imdb:tt1"}, ["kids"])])  # tag added
    poller.poll()
    assert item.labels() == {"kids-allowed"}


def test_show_tag_change_uses_sonarr_media_type():
    show = FakePlexItem("Wednesday", "show", set(), {"tvdb:111"}, rating_key="9")
    sonarr = FakeArr([({"tvdb:111"}, [])])
    poller = make_poller(FakePlex([show]), sonarr=sonarr, now=datetime(2026, 1, 1))
    poller.poll()
    sonarr.set([({"tvdb:111"}, ["family"])])
    poller.poll()
    assert show.labels() == {"shared"}


# --- recently-added polling -------------------------------------------------

def test_recently_added_baselines_then_labels_new_item():
    t0 = datetime(2026, 1, 1, 0, 0, 0)
    show = FakePlexItem(
        "Wednesday", "show", set(), {"tvdb:111"}, rating_key="9",
        added_at=t0 + timedelta(minutes=5),
    )
    plex = FakePlex(items=[show], recent=[])
    sonarr = FakeArr([({"tvdb:111"}, ["family"])])
    poller = make_poller(plex, sonarr=sonarr, now=t0)
    poller.poll()                            # baseline: watermark = t0
    plex._recent = [show]                    # show appears after baseline
    summary = poller.poll()
    assert show.labels() == {"shared"}
    assert summary["added"] == 1
    assert "Wednesday" in summary["added_titles"]


def test_recently_added_ignores_items_at_or_before_watermark():
    t0 = datetime(2026, 1, 1)
    old = FakePlexItem(
        "Old", "movie", set(), {"tmdb:1"}, rating_key="1",
        added_at=t0 - timedelta(days=1),
    )
    plex = FakePlex(items=[old], recent=[old])
    radarr = FakeArr([({"tmdb:1"}, ["kids"])])
    poller = make_poller(plex, radarr=radarr, now=t0)
    poller.poll()                            # baseline watermark t0; old is before it
    summary = poller.poll()
    assert summary["added"] == 0
    assert old.labels() == set()


# --- failures are retried, not dropped ----------------------------------------

import requests  # noqa: E402

from app.core.reactive import MAX_ATTEMPTS  # noqa: E402


class FlakyItem(FakePlexItem):
    """A Plex item whose label edits raise ``exc`` for the first ``fails`` attempts."""

    def __init__(self, *args, fails=1, exc=RuntimeError("boom"), **kwargs):
        super().__init__(*args, **kwargs)
        self.fails, self.exc, self.attempts = fails, exc, 0

    def add_labels(self, labels):
        self.attempts += 1
        if self.attempts <= self.fails:
            raise self.exc
        super().add_labels(labels)


class RefetchPlex(FakePlex):
    def fetch_labelable(self, rating_key):
        for item in self._items:
            if item.rating_key == rating_key:
                return item
        return None


def _two_movies(first):
    second = FakePlexItem("Arrival", "movie", set(), {"tmdb:2"}, rating_key="2")
    radarr = FakeArr([({"tmdb:1"}, []), ({"tmdb:2"}, [])])
    poller = make_poller(FakePlex([first, second]), radarr=radarr, now=datetime(2026, 1, 1))
    poller.poll()  # baseline
    radarr.set([({"tmdb:1"}, ["kids"]), ({"tmdb:2"}, ["kids"])])
    return poller, second


def test_failed_tag_change_is_retried_next_poll_and_others_still_apply():
    first = FlakyItem("Dune", "movie", set(), {"tmdb:1"}, rating_key="1")
    poller, second = _two_movies(first)

    poller.poll()
    assert first.labels() == set()            # failed this poll...
    assert second.labels() == {"kids-allowed"}  # ...without stopping the other change

    poller.poll()
    assert first.labels() == {"kids-allowed"}  # retried and applied


def test_plex_unreachable_mid_poll_keeps_every_change_for_next_poll():
    first = FlakyItem("Dune", "movie", set(), {"tmdb:1"}, rating_key="1",
                      exc=requests.exceptions.ConnectionError("refused"))
    poller, second = _two_movies(first)

    poller.poll()
    assert first.labels() == set() and second.labels() == set()  # stopped at Plex down

    poller.poll()
    assert first.labels() == {"kids-allowed"}
    assert second.labels() == {"kids-allowed"}


def test_plex_outage_does_not_use_up_attempts():
    first = FlakyItem("Dune", "movie", set(), {"tmdb:1"}, rating_key="1",
                      fails=MAX_ATTEMPTS + 2, exc=requests.exceptions.ConnectionError("down"))
    poller, _ = _two_movies(first)
    for _ in range(MAX_ATTEMPTS + 3):
        poller.poll()
    assert first.labels() == {"kids-allowed"}  # applied once Plex was back


def test_change_that_keeps_failing_is_left_to_the_sweep():
    first = FlakyItem("Dune", "movie", set(), {"tmdb:1"}, rating_key="1", fails=10**6)
    poller, _ = _two_movies(first)
    for _ in range(MAX_ATTEMPTS + 3):
        poller.poll()
    assert first.attempts == MAX_ATTEMPTS  # no retry storm every minute


def test_failed_recently_added_item_is_refetched_and_retried():
    t0 = datetime(2026, 1, 1)
    movie = FlakyItem("Dune", "movie", set(), {"tmdb:1"}, rating_key="1",
                      added_at=t0 + timedelta(minutes=5))
    plex = RefetchPlex(items=[movie], recent=[])
    poller = make_poller(plex, radarr=FakeArr([({"tmdb:1"}, ["kids"])]), now=t0)
    poller.poll()             # baseline
    plex._recent = [movie]

    assert poller.poll()["added"] == 0  # reconcile failed
    plex._recent = []                   # the watermark has moved past it
    assert poller.poll()["added_titles"] == ["Dune"]
    assert movie.labels() == {"kids-allowed"}


def test_recently_added_item_is_retried_after_plex_outage():
    t0 = datetime(2026, 1, 1)
    movie = FlakyItem("Dune", "movie", set(), {"tmdb:1"}, rating_key="1",
                      added_at=t0 + timedelta(minutes=5),
                      fails=MAX_ATTEMPTS + 2, exc=requests.exceptions.ConnectionError("down"))
    plex = RefetchPlex(items=[movie], recent=[])
    poller = make_poller(plex, radarr=FakeArr([({"tmdb:1"}, ["kids"])]), now=t0)
    poller.poll()
    plex._recent = [movie]
    poller.poll()
    plex._recent = []
    for _ in range(MAX_ATTEMPTS + 2):
        poller.poll()
    assert movie.labels() == {"kids-allowed"}
