"""An unreachable Plex must never stop the app from starting.

Regression: PlexClient connected in ``__init__``, so a Plex URL that didn't
resolve (typically a ``*.plex.direct`` name blocked by DNS-rebinding protection)
raised out of build_components and crash-looped the container — taking the UI,
the one place to fix the URL, down with it.
"""

import socket

import pytest
import requests
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from urllib3.exceptions import MaxRetryError, NameResolutionError

import app.clients.plex as plex_mod
import app.main as main
from app.clients.plex import (
    PlexClient,
    PlexUnavailableError,
    describe_connect_error,
    plex_direct_fallback_url,
)
from app.config import load_settings
from app.webhook import create_app

PLEX_DIRECT = "https://10-0-0-11.6626d26d0f1b425c925e4bf2ba17c930.plex.direct:32400"


def _dns_error(host: str) -> requests.exceptions.ConnectionError:
    """The exception chain requests raises when a hostname doesn't resolve."""
    gai = socket.gaierror(-5, "No address associated with hostname")
    nre = NameResolutionError(host, None, gai)
    nre.__cause__ = gai
    mre = MaxRetryError(None, "/", nre)
    mre.__cause__ = nre
    return requests.exceptions.ConnectionError(mre)


class FakeServer:
    friendlyName = "MyPlex"

    def __init__(self, *_args, **_kwargs):
        pass


@pytest.fixture
def plex_server(monkeypatch):
    """Patch plexapi's PlexServer; ``state["fail"]`` makes it raise that error."""
    state = {"calls": 0, "fail": _dns_error("10-0-0-11.x.plex.direct")}

    def factory(*args, **kwargs):
        state["calls"] += 1
        if state["fail"] is not None:
            raise state["fail"]
        return FakeServer(*args, **kwargs)

    monkeypatch.setattr("plexapi.server.PlexServer", factory)
    return state


def test_constructing_client_does_no_network(plex_server):
    PlexClient(PLEX_DIRECT, "tok")
    assert plex_server["calls"] == 0


def test_connect_raises_readable_dns_error(plex_server):
    client = PlexClient(PLEX_DIRECT, "tok")
    with pytest.raises(PlexUnavailableError) as info:
        client.connect()
    assert info.value.reason == "dns"
    message = str(info.value)
    assert "doesn't resolve" in message
    assert "DNS-rebinding" in message
    assert "http://10.0.0.11:32400" in message  # concrete fix, derived from the name


def test_connect_backs_off_then_retries_and_recovers(plex_server, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(plex_mod.time, "monotonic", lambda: clock["now"])
    client = PlexClient(PLEX_DIRECT, "tok")

    with pytest.raises(PlexUnavailableError):
        client.connect()
    with pytest.raises(PlexUnavailableError):  # inside the back-off: no new attempt
        client.connect()
    assert plex_server["calls"] == 1

    plex_server["fail"] = None  # e.g. DNS fixed / Plex back up
    clock["now"] += plex_mod._CONNECT_RETRY_SECONDS + 1
    server = client.connect()
    assert server.friendlyName == "MyPlex"
    assert client.connect() is server  # cached once connected
    assert plex_server["calls"] == 2


def test_check_reports_dns_failure_without_leaking_url(plex_server):
    out = PlexClient(PLEX_DIRECT, "tok").check()
    assert out == {"ok": False, "detail": "DNS lookup failed"}


def test_check_connects_lazily_when_reachable(plex_server):
    plex_server["fail"] = None
    assert PlexClient("http://plex:32400", "tok").check() == {"ok": True, "detail": "MyPlex"}


def test_unauthorized_is_reported_as_such():
    from plexapi.exceptions import Unauthorized

    err = describe_connect_error("http://plex:32400", Unauthorized("(401) unauthorized"))
    assert err.reason == "unauthorized"
    assert "Sign in with Plex" in str(err)


def test_other_failures_are_generic():
    err = describe_connect_error("http://plex:32400", requests.exceptions.ConnectTimeout())
    assert err.reason == "unreachable"
    assert "ConnectTimeout" in str(err)
    assert "plex.direct" not in str(err)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (PLEX_DIRECT, "http://10.0.0.11:32400"),
        ("https://192-168-1-20.abc.plex.direct:32401", "http://192.168.1.20:32401"),
        ("https://999-0-0-1.abc.plex.direct:32400", None),  # not a valid IPv4
        ("http://plex:32400", None),
        ("https://my.plex.example:32400", None),
    ],
)
def test_plex_direct_fallback_url(url, expected):
    assert plex_direct_fallback_url(url) == expected


# --- wiring -------------------------------------------------------------------


def test_job_is_skipped_while_plex_is_down():
    class DownPlex:
        def connect(self):
            raise PlexUnavailableError("down")

    ran = []
    runner = main._when_plex_reachable(DownPlex(), "sweep", lambda: ran.append(1) or {})
    assert runner() is None  # None -> the tracker records nothing
    assert ran == []


def test_job_runs_when_plex_is_up():
    class UpPlex:
        def connect(self):
            return object()

    runner = main._when_plex_reachable(UpPlex(), "sweep", lambda: {"changed": 1})
    assert runner() == {"changed": 1}


def test_app_starts_and_serves_when_plex_is_unreachable(plex_server, monkeypatch, tmp_path):
    env = {
        "FEATURE_UI": "true",
        "PAL_SECRET_KEY": Fernet.generate_key().decode(),
        "CONFIG_PATH": str(tmp_path / "config.json"),
        "STATE_DB_PATH": str(tmp_path / "state.db"),
        "ONBOARDING_COMPLETE": "true",
        "PLEX_URL": PLEX_DIRECT,
        "PLEX_TOKEN": "tok",
        "RADARR_URL": "http://radarr:7878",
        "RADARR_API_KEY": "rk",
        "SONARR_URL": "http://sonarr:8989",
        "SONARR_API_KEY": "sk",
        "TAG_LABEL_MAP": "kids:kids-allowed",
        "FEATURE_SWEEP": "false",
        "FEATURE_REACTIVE": "false",
        "FEATURE_NOTIFY": "false",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    app = main.create_application(load_settings())  # used to raise ConnectionError
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        status = client.get("/api/status").json()
        assert status["configured"] is True
        assert status["connections"]["plex"] == {"ok": False, "detail": "DNS lookup failed"}


def test_webhooks_return_503_while_plex_is_down():
    class DownSync:
        def sync_by_ids(self, *_args, **_kwargs):
            raise PlexUnavailableError("down")

        def sync_by_rating_key(self, _rating_key):
            raise PlexUnavailableError("down")

    client = TestClient(
        create_app(lambda: DownSync(), webhook_path="/webhook/seerr",
                   plex_webhook_path="/webhook/plex")
    )
    seerr = client.post(
        "/webhook/seerr",
        json={"notification_type": "MEDIA_AVAILABLE",
              "media": {"media_type": "movie", "tmdbId": "603"}},
    )
    assert seerr.status_code == 503
    plex = client.post(
        "/webhook/plex",
        data={"payload": '{"event": "library.new", "Metadata": {"ratingKey": "1"}}'},
    )
    assert plex.status_code == 503
