"""``*.plex.direct`` URLs connect without DNS, with TLS still checked for the name.

Regression: Sign in with Plex saves a URL like
``https://10-0-0-11.<id>.plex.direct:32400``. Resolvers with DNS-rebinding
protection (Pi-hole, AdGuard Home, Unbound, most routers) won't resolve it, so
Plex was never reached and every job was skipped. The name already encodes the
IP, so the client connects to that IP directly.

These tests run a real HTTPS server (cert from a throwaway CA) and the real
plexapi client, with DNS for plex.direct made to fail.
"""

import datetime
import http.server
import socket
import ssl
import threading

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.clients.plex import (
    PlexClient,
    PlexUnavailableError,
    _PlexDirectAdapter,
    plex_direct_ip,
    plex_session,
)

PLEX_ID = "6626d26d0f1b425c925e4bf2ba17c930"
XML = b'<?xml version="1.0"?><MediaContainer friendlyName="TestPlex" machineIdentifier="m1"/>'


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _make_certs(tmp_path, san: str):
    """A CA (written to ca.pem) and a leaf cert for ``san`` signed by it."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(_name("Test CA"))
        .issuer_name(_name("Test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name(san))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = tmp_path / "ca.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path = tmp_path / "leaf.pem"
    cert_path.write_bytes(
        leaf.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path


@pytest.fixture
def no_plex_direct_dns(monkeypatch):
    """Fail every DNS lookup of a plex.direct name, like a rebind-protecting resolver."""
    real = socket.getaddrinfo
    looked_up = []

    def getaddrinfo(host, *args, **kwargs):
        if isinstance(host, str) and host.endswith(".plex.direct"):
            looked_up.append(host)
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return looked_up


@pytest.fixture
def no_proxy_env(monkeypatch):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def https_plex(tmp_path, monkeypatch, no_proxy_env):
    """Start a TLS 'Plex' on 127.0.0.1. Returns a function: cert SAN -> (url, seen)."""
    servers = []

    def start(san: str):
        ca_path, cert_path = _make_certs(tmp_path, san)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_path))
        seen = {"host": [], "token": []}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                seen["host"].append(self.headers.get("Host"))
                seen["token"].append(self.headers.get("X-Plex-Token"))
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Content-Length", str(len(XML)))
                self.end_headers()
                self.wfile.write(XML)

            def log_message(self, *_args):
                pass

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert_path)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        port = httpd.server_address[1]
        return f"https://127-0-0-1.{PLEX_ID}.plex.direct:{port}", seen

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def test_connects_to_plex_direct_url_when_dns_is_blocked(https_plex, no_plex_direct_dns):
    url, seen = https_plex(f"*.{PLEX_ID}.plex.direct")  # Plex's real certs are wildcards

    server = PlexClient(url, "secret-token").connect()

    assert server.friendlyName == "TestPlex"
    assert no_plex_direct_dns == []  # never asked DNS
    # Same request DNS would have produced: Host is the plex.direct name, not the IP.
    assert seen["host"] == [url.removeprefix("https://")]
    assert seen["token"] == ["secret-token"]


def test_certificate_is_still_verified_against_the_name(https_plex, no_plex_direct_dns):
    url, _ = https_plex("*.someone-else.plex.direct")  # valid CA, wrong name

    with pytest.raises(PlexUnavailableError) as info:
        PlexClient(url, "tok").connect()
    assert info.value.reason == "unreachable"
    assert isinstance(info.value.__cause__, requests.exceptions.SSLError)


def test_untrusted_certificate_is_rejected(https_plex, no_plex_direct_dns, monkeypatch):
    url, _ = https_plex(f"*.{PLEX_ID}.plex.direct")
    monkeypatch.delenv("REQUESTS_CA_BUNDLE")  # our test CA is no longer trusted

    with pytest.raises(PlexUnavailableError) as info:
        PlexClient(url, "tok").connect()
    assert isinstance(info.value.__cause__, requests.exceptions.SSLError)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://10-0-0-11.{PLEX_ID}.plex.direct:32400", "10.0.0.11"),
        ("https://192-168-1-20.abc.PLEX.DIRECT:32401", "192.168.1.20"),
        ("https://999-0-0-1.abc.plex.direct:32400", None),  # not a valid IPv4
        ("https://10-0-0-11.plex.direct:32400", None),  # no server id label
        ("http://plex:32400", None),
        ("http://10.0.0.11:32400", None),
        ("https://my.plex.example:32400", None),
    ],
)
def test_plex_direct_ip(url, expected):
    assert plex_direct_ip(url) == expected


def test_other_urls_use_plexapis_default_session(monkeypatch):
    sessions = []

    class FakeServer:
        friendlyName = "MyPlex"

        def __init__(self, _url, _token, session=None):
            sessions.append(session)

    monkeypatch.setattr("plexapi.server.PlexServer", FakeServer)
    PlexClient("http://10.0.0.11:32400", "tok").connect()
    PlexClient(f"https://10-0-0-11.{PLEX_ID}.plex.direct:32400", "tok").connect()
    assert sessions[0] is None
    assert isinstance(sessions[1], requests.Session)
    assert plex_session("http://plex:32400") is None


def _capturing_adapter(monkeypatch):
    sent = []

    def fake_send(self, request, **kwargs):
        sent.append(request)
        response = requests.Response()
        response.status_code = 200
        response.url, response.request = request.url, request
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
    return _PlexDirectAdapter(f"10-0-0-11.{PLEX_ID}.plex.direct", "10.0.0.11"), sent


def _prepared(url):
    return requests.Request("GET", url).prepare()


def test_adapter_rewrites_only_its_own_host(monkeypatch):
    adapter, sent = _capturing_adapter(monkeypatch)
    url = f"https://10-0-0-11.{PLEX_ID}.plex.direct:32400/library/sections?x=1"

    response = adapter.send(_prepared(url), proxies={})
    assert sent[-1].url == "https://10.0.0.11:32400/library/sections?x=1"
    assert sent[-1].headers["Host"] == f"10-0-0-11.{PLEX_ID}.plex.direct:32400"
    assert response.url == url  # caller (and redirects) see the original URL
    assert "Host" not in response.request.headers  # caller's request left untouched

    other = "https://10-0-0-12.other.plex.direct:32400/"
    adapter.send(_prepared(other), proxies={})
    assert sent[-1].url == other


def test_adapter_leaves_proxied_requests_alone(monkeypatch):
    adapter, sent = _capturing_adapter(monkeypatch)
    url = f"https://10-0-0-11.{PLEX_ID}.plex.direct:32400/"

    adapter.send(_prepared(url), proxies={"https": "http://proxy:3128"})
    assert sent[-1].url == url  # the proxy resolves the name
    assert "Host" not in sent[-1].headers
