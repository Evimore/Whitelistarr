"""Label edits through real plexapi objects, for items resolved by the fallback index.

plexapi never refreshes an object's labels after an edit, and ``addLabel`` sends
the object's labels plus the new one as the complete list. These tests use real
plexapi ``Movie``/``MovieSection`` objects against a fake server that holds the
true label state and applies plexapi's PUT queries the way Plex does, so they
fail if a stale object is ever reused for an edit.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import parse_qsl, urlsplit

from plexapi.exceptions import NotFound
from plexapi.library import MovieSection
from plexapi.video import Movie

from app.clients.plex import PlexClient
from app.core.sync import LabelSync

LABEL_MAP = {"kids": "kids-allowed", "family": "shared"}
# Legacy agent: only an imdb guid, so getGuid misses and the fallback index resolves it.
_LEGACY_GUID = "com.plexapp.agents.imdb://tt29768334?lang=en"
_ARR_KEYS = {"tmdb:29768334", "imdb:tt29768334"}


class _Session:
    def get(self, *args, **kwargs): ...

    def put(self, *args, **kwargs): ...


class FakePlexServer:
    """Holds the authoritative labels of one movie and applies label PUTs."""

    def __init__(self, labels: list[str]) -> None:
        self._session = _Session()
        self._baseurl, self._token = "http://plex:32400", "token"
        self.labels = list(labels)
        self.section: MovieSection | None = None
        library = type("Library", (), {})()
        library.sections = lambda: [self.section]
        library.sectionByID = lambda _id: self.section
        self.library = library

    def _container(self) -> ET.Element:
        tags = "".join(f'<Label tag="{t}"/>' for t in self.labels)
        return ET.fromstring(
            '<MediaContainer><Video ratingKey="1" key="/library/metadata/1" type="movie"'
            f' title="Train Dreams" librarySectionID="1" guid="{_LEGACY_GUID}">{tags}'
            "</Video></MediaContainer>"
        )

    def movie(self) -> Movie:
        return Movie(self, self._container()[0], initpath="/library/metadata/1")

    def fetchItem(self, ekey):
        return self.movie()

    def query(self, key, method=None, **kwargs):
        if method != self._session.put:
            return self._container()
        params = parse_qsl(urlsplit(key).query)
        indexed = [v for k, v in params if k.startswith("label[") and k.endswith("].tag.tag")]
        if indexed:  # label[0], label[1], ... replaces the whole list
            self.labels = indexed
        for k, v in params:
            if k == "label[].tag.tag-":
                drop = {t.casefold() for t in v.split(",")}
                self.labels = [t for t in self.labels if t.casefold() not in drop]
        return None


class FakeArr:
    def __init__(self, tags: list[str]) -> None:
        self.tags = tags

    def iter_all_with_tags(self):
        return [(set(_ARR_KEYS), list(self.tags))] if self.tags is not None else []


def _setup(plex_labels: list[str], arr_tags: list[str]):
    server = FakePlexServer(plex_labels)
    section = MovieSection(
        server,
        ET.fromstring('<Directory key="1" type="movie" title="Movies" agent="com.plexapp.agents.imdb"/>'),
        initpath="/library/sections",
    )

    def get_guid(guid):
        raise NotFound(guid)

    section.getGuid = get_guid
    section.all = lambda: [server.movie()]
    server.section = section

    plex = object.__new__(PlexClient)
    plex._server = server
    plex._section_filter = set()
    plex._resolve_index = None
    plex._resolve_index_at = 0.0
    plex._resolve_ttl = 300.0

    radarr = FakeArr(arr_tags)
    sync = LabelSync(
        plex=plex, radarr=radarr, sonarr=FakeArr(None),
        label_map=LABEL_MAP, managed=set(LABEL_MAP.values()),
    )
    return server, radarr, sync


def test_label_is_removed_when_tag_removed_soon_after_it_was_added():
    server, radarr, sync = _setup(["Favorite"], ["kids"])
    sync.sync_by_ids("movie", tmdb_id=29768334)
    assert server.labels == ["Favorite", "kids-allowed"]

    radarr.tags = []  # tag removed in Radarr within the index's cache window
    sync.sync_by_ids("movie", tmdb_id=29768334)

    assert server.labels == ["Favorite"]


def test_labels_added_in_plex_survive_a_later_label_add():
    server, radarr, sync = _setup(["Favorite"], ["kids"])
    sync.sync_by_ids("movie", tmdb_id=29768334)
    server.labels.append("Watchlist")  # added by hand in Plex

    radarr.tags = ["kids", "family"]
    sync.sync_by_ids("movie", tmdb_id=29768334)

    assert server.labels == ["Favorite", "kids-allowed", "Watchlist", "shared"]
