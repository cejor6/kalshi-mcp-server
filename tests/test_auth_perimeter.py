"""Regression tests for the HTTP auth perimeter.

Covers: the GitHub allowlist applying to EVERY MCP request (not only tool
calls), id-based allowlisting, stdio not getting the allowlist, partial OAuth
config failing closed, the insecure-HTTP bind/Host guards, and startup
refusals surfacing as a clean error rather than a traceback.

Nothing here touches the network: the MCP traffic is an in-memory FastMCP
client, `get_access_token` is patched, and the ASGI guard is driven through
`httpx.ASGITransport`.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from mcp.shared.exceptions import McpError

from kalshi_mcp_server import oauth
from kalshi_mcp_server.cli import _enforce_http_auth_policy, main
from kalshi_mcp_server.errors import ConfigError
from kalshi_mcp_server.http_guard import (
    HostOriginGuard,
    is_loopback_bind,
    parse_allowed_hosts_env,
)
from kalshi_mcp_server.oauth import (
    ALLOWLIST_ENV_VARS,
    OAUTH_CORE_ENV_VARS,
    OAUTH_ENV_VARS,
    RestrictGitHubUsersMiddleware,
    _parse_allowed_ids,
    build_auth_provider,
    build_user_restriction_middleware,
)

_ENV_VARS = (
    *OAUTH_ENV_VARS,
    "MCP_JWT_SIGNING_KEY",
    "MCP_REDIS_URL",
    "MCP_REDIS_COLLECTION_PREFIX",
    "MCP_ALLOW_INSECURE_HTTP",
    "MCP_ALLOW_INSECURE_PUBLIC_BIND",
    "MCP_ALLOWED_HOSTS",
    "MCP_TRANSPORT",
    "PORT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _set_oauth(monkeypatch):
    monkeypatch.setenv("GITHUB_CLIENT_ID", "id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MCP_BASE_URL", "https://example.com")


# ── allowlist middleware: every request, not just tool calls ──────────────


class _TokenHolder:
    """Mutable stand-in for the caller's identity, swapped mid-session."""

    def __init__(self, login: str | None, sub: str | None) -> None:
        self.set(login, sub)

    def set(self, login: str | None, sub: str | None) -> None:
        claims = {}
        if login is not None:
            claims["login"] = login
        if sub is not None:
            claims["sub"] = sub
        self.token = AccessToken(token="t", client_id="c", scopes=[], claims=claims)

    def __call__(self):
        return self.token


def _server(middleware) -> FastMCP:
    server = FastMCP("perimeter-test")

    @server.tool
    def kalshi_ping() -> str:
        return "pong"

    @server.resource("kalshi://balance")
    def balance() -> str:
        return '{"balance": 100}'

    server.add_middleware(middleware)
    return server


@pytest.fixture
def identity(monkeypatch):
    holder = _TokenHolder(login="alice", sub="1001")
    monkeypatch.setattr(oauth, "get_access_token", holder)
    return holder


@pytest.fixture
def allowlisted_server():
    return _server(RestrictGitHubUsersMiddleware(frozenset({"alice"}), frozenset({"4242"})))


async def test_allowlisted_login_can_use_everything(identity, allowlisted_server):
    async with Client(allowlisted_server) as client:
        assert [t.name for t in await client.list_tools()] == ["kalshi_ping"]
        assert [str(r.uri) for r in await client.list_resources()] == ["kalshi://balance"]
        contents = await client.read_resource("kalshi://balance")
        assert "100" in contents[0].text
        result = await client.call_tool("kalshi_ping", {})
        assert result.data == "pong"


async def test_non_allowlisted_user_is_refused_on_every_request(identity, allowlisted_server):
    # Connect as an allowed user, then become an outsider: exercises each
    # request type individually (initialize itself is covered below).
    async with Client(allowlisted_server) as client:
        identity.set(login="mallory", sub="666")

        with pytest.raises(McpError, match="mallory"):
            await client.list_tools()
        with pytest.raises(McpError, match="not authorized"):
            await client.list_resources()
        with pytest.raises(McpError, match="not authorized"):
            await client.list_resource_templates()
        with pytest.raises(McpError, match="not authorized"):
            await client.read_resource("kalshi://balance")
        # Tool calls get a tool error, which the model sees as a normal
        # failed call rather than a protocol error.
        result = await client.call_tool_mcp("kalshi_ping", {})
        assert result.isError
        assert "mallory" in result.content[0].text
        assert "not authorized" in result.content[0].text
        with pytest.raises(ToolError, match="not authorized"):
            await client.call_tool("kalshi_ping", {})


async def test_resource_read_was_the_hole(identity, allowlisted_server):
    """The original bug: only on_call_tool was guarded, so balance leaked."""
    async with Client(allowlisted_server) as client:
        identity.set(login="mallory", sub="666")
        with pytest.raises(McpError) as exc:
            await client.read_resource("kalshi://balance")
        assert "100" not in str(exc.value)
        assert exc.value.error.code == oauth.FORBIDDEN_ERROR_CODE


async def test_non_allowlisted_user_cannot_even_initialize(identity, allowlisted_server):
    identity.set(login="mallory", sub="666")
    with pytest.raises(Exception, match="not authorized"):
        async with Client(allowlisted_server):
            pass


async def test_missing_token_is_refused(monkeypatch, allowlisted_server):
    monkeypatch.setattr(oauth, "get_access_token", lambda: None)
    with pytest.raises(Exception, match="Unauthenticated"):
        async with Client(allowlisted_server):
            pass


async def test_allowlisted_by_numeric_id_survives_a_rename(identity, allowlisted_server):
    identity.set(login="alice-renamed", sub="4242")
    async with Client(allowlisted_server) as client:
        contents = await client.read_resource("kalshi://balance")
        assert "100" in contents[0].text


async def test_id_list_alone_does_not_admit_a_login(identity):
    # A login that *looks* like an allowlisted id must not match it.
    server = _server(RestrictGitHubUsersMiddleware(frozenset(), frozenset({"4242"})))
    identity.set(login="4242", sub="999")
    with pytest.raises(Exception, match="not authorized"):
        async with Client(server):
            pass


async def test_login_match_is_case_insensitive(identity, allowlisted_server):
    identity.set(login="ALICE", sub="1")
    async with Client(allowlisted_server) as client:
        assert await client.list_tools()


async def test_empty_allowlists_fail_closed(identity):
    server = _server(RestrictGitHubUsersMiddleware(frozenset(), frozenset()))
    with pytest.raises(Exception, match="misconfigured"):
        async with Client(server):
            pass


# ── MCP_ALLOWED_GITHUB_IDS parsing ────────────────────────────────────────


def test_parse_allowed_ids(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_GITHUB_IDS", " 123, 0456 ,,789 ")
    assert _parse_allowed_ids() == frozenset({"123", "456", "789"})


@pytest.mark.parametrize("bad", ["alice", "12a", "-5", "1.0", "١٢٣"])
def test_parse_allowed_ids_rejects_non_numeric(monkeypatch, bad):
    monkeypatch.setenv("MCP_ALLOWED_GITHUB_IDS", f"123,{bad}")
    with pytest.raises(ConfigError, match="not a numeric GitHub user id"):
        _parse_allowed_ids()


def test_middleware_built_from_ids_only(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_GITHUB_IDS", "4242")
    assert isinstance(build_user_restriction_middleware(), RestrictGitHubUsersMiddleware)


def test_http_policy_accepts_ids_without_logins(monkeypatch):
    _set_oauth(monkeypatch)
    monkeypatch.setenv("MCP_ALLOWED_GITHUB_IDS", "4242")
    provider, _ = build_auth_provider()
    _enforce_http_auth_policy(auth_provider=provider)


def test_http_policy_rejects_malformed_ids(monkeypatch):
    _set_oauth(monkeypatch)
    monkeypatch.setenv("MCP_ALLOWED_GITHUB_LOGINS", "alice")
    monkeypatch.setenv("MCP_ALLOWED_GITHUB_IDS", "alice")
    provider, _ = build_auth_provider()
    with pytest.raises(ConfigError, match="numeric"):
        _enforce_http_auth_policy(auth_provider=provider)


# ── partial OAuth config fails closed ─────────────────────────────────────


@pytest.mark.parametrize(
    "present",
    [
        ("GITHUB_CLIENT_ID",),
        ("GITHUB_CLIENT_SECRET",),
        ("MCP_BASE_URL",),
        ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"),
        ("GITHUB_CLIENT_ID", "MCP_BASE_URL"),
        ("GITHUB_CLIENT_SECRET", "MCP_BASE_URL"),
    ],
)
def test_partial_oauth_config_raises(monkeypatch, present):
    for name in present:
        monkeypatch.setenv(name, "https://example.com" if name == "MCP_BASE_URL" else "x")
    with pytest.raises(ConfigError, match="Partial OAuth configuration") as exc:
        build_auth_provider()
    for name in set(OAUTH_CORE_ENV_VARS) - set(present):
        assert name in str(exc.value)


def test_whitespace_only_oauth_var_counts_as_unset(monkeypatch):
    monkeypatch.setenv("GITHUB_CLIENT_ID", "   ")
    assert build_auth_provider() == (None, "")


# ── insecure-HTTP policy ──────────────────────────────────────────────────


@pytest.mark.parametrize("var", OAUTH_CORE_ENV_VARS)
def test_insecure_flag_with_partial_oauth_refuses(monkeypatch, var):
    monkeypatch.setenv("MCP_ALLOW_INSECURE_HTTP", "1")
    monkeypatch.setenv(var, "123")
    with pytest.raises(ConfigError, match="partial OAuth"):
        _enforce_http_auth_policy(auth_provider=None)
    # ...and on the real path build_auth_provider refuses first.
    with pytest.raises(ConfigError, match="Partial OAuth configuration"):
        build_auth_provider()


def test_insecure_flag_with_full_oauth_is_ignored_with_a_warning(monkeypatch, caplog):
    """A prod OAuth deploy still carrying Phase A's flag must keep booting —
    with OAuth on, not off."""
    _set_oauth(monkeypatch)
    monkeypatch.setenv("MCP_ALLOWED_GITHUB_LOGINS", "alice")
    monkeypatch.setenv("MCP_ALLOW_INSECURE_HTTP", "1")
    provider, _ = build_auth_provider()
    assert provider is not None
    with caplog.at_level(logging.WARNING, logger="kalshi_mcp_server"):
        _enforce_http_auth_policy(auth_provider=provider, bind_host="0.0.0.0")  # noqa: S104
    assert "MCP_ALLOW_INSECURE_HTTP=1 is IGNORED" in caplog.text


def test_insecure_flag_with_full_oauth_still_requires_allowlist(monkeypatch):
    _set_oauth(monkeypatch)
    monkeypatch.setenv("MCP_ALLOW_INSECURE_HTTP", "1")
    provider, _ = build_auth_provider()
    with pytest.raises(ConfigError, match="no GitHub allowlist"):
        _enforce_http_auth_policy(auth_provider=provider)


@pytest.mark.parametrize("var", ALLOWLIST_ENV_VARS)
def test_insecure_loopback_with_allowlist_var_runs_and_warns(monkeypatch, caplog, var):
    """A shared .env carrying an allowlist mustn't block local insecure HTTP."""
    monkeypatch.setenv("MCP_ALLOW_INSECURE_HTTP", "1")
    monkeypatch.setenv(var, "123")
    with caplog.at_level(logging.WARNING, logger="kalshi_mcp_server"):
        _enforce_http_auth_policy(auth_provider=None, bind_host="127.0.0.1")
    assert f"{var} set but NOT enforced" in caplog.text


def test_insecure_flag_set_to_zero_is_not_a_conflict(monkeypatch):
    _set_oauth(monkeypatch)
    monkeypatch.setenv("MCP_ALLOWED_GITHUB_LOGINS", "alice")
    monkeypatch.setenv("MCP_ALLOW_INSECURE_HTTP", "0")
    provider, _ = build_auth_provider()
    _enforce_http_auth_policy(auth_provider=provider)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com", ""])  # noqa: S104
def test_insecure_http_refuses_non_loopback_bind(monkeypatch, host):
    monkeypatch.setenv("MCP_ALLOW_INSECURE_HTTP", "1")
    with pytest.raises(ConfigError, match="MCP_ALLOW_INSECURE_PUBLIC_BIND"):
        _enforce_http_auth_policy(auth_provider=None, bind_host=host)


def test_insecure_http_public_bind_needs_second_flag(monkeypatch):
    monkeypatch.setenv("MCP_ALLOW_INSECURE_HTTP", "1")
    monkeypatch.setenv("MCP_ALLOW_INSECURE_PUBLIC_BIND", "1")
    _enforce_http_auth_policy(auth_provider=None, bind_host="0.0.0.0")  # noqa: S104


def test_public_bind_flag_alone_does_not_enable_insecure_http(monkeypatch):
    monkeypatch.setenv("MCP_ALLOW_INSECURE_PUBLIC_BIND", "1")
    with pytest.raises(ConfigError, match="requires OAuth"):
        _enforce_http_auth_policy(auth_provider=None, bind_host="0.0.0.0")  # noqa: S104


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2"])
def test_insecure_http_allows_loopback_bind(monkeypatch, host):
    monkeypatch.setenv("MCP_ALLOW_INSECURE_HTTP", "1")
    _enforce_http_auth_policy(auth_provider=None, bind_host=host)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("LOCALHOST", True),
        ("::1", True),
        ("0.0.0.0", False),  # noqa: S104
        ("::", False),
        ("10.0.0.1", False),
        ("localhost.evil.com", False),
        ("", False),
    ],
)
def test_is_loopback_bind(host, expected):
    assert is_loopback_bind(host) is expected


# ── DNS-rebinding guard ───────────────────────────────────────────────────


async def _ok_app(scope, receive, send):
    if scope["type"] == "lifespan":  # pragma: no cover — not exercised here
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _guarded_client(base_url: str, **guard_kwargs) -> httpx.AsyncClient:
    app = HostOriginGuard(_ok_app, **guard_kwargs)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://127.0.0.2:8000",
        "http://127.255.0.9",
        "http://[::1]:8000",
        "http://localhost",
    ],
)
async def test_guard_allows_loopback_hosts(base_url):
    async with _guarded_client(base_url) as client:
        resp = await client.post("/mcp", content=b"{}")
    assert resp.status_code == 200


async def test_guard_rejects_rebound_hostname():
    async with _guarded_client("http://attacker.example:8000") as client:
        resp = await client.post("/mcp", content=b"{}")
    assert resp.status_code == 421


async def test_guard_rejects_foreign_origin_even_with_loopback_host():
    async with _guarded_client("http://localhost:8000") as client:
        resp = await client.post(
            "/mcp", content=b"{}", headers={"Origin": "http://attacker.example"}
        )
        assert resp.status_code == 403
        resp = await client.post("/mcp", content=b"{}", headers={"Origin": "null"})
        assert resp.status_code == 403
        resp = await client.post("/mcp", content=b"{}", headers={"Origin": "http://localhost:3000"})
        assert resp.status_code == 200


async def test_guard_rejects_malformed_host():
    async with _guarded_client("http://localhost:8000") as client:
        resp = await client.get("/mcp", headers={"Host": "localhost@attacker.example"})
    assert resp.status_code == 421


@pytest.mark.parametrize(
    "host",
    [
        b"local\thost",
        b"localhost\t",
        b"\tlocalhost",
        b"127.0.0.1\r\n",
        b"128.0.0.1",
        b"127.0.0.1.evil",
    ],
)
async def test_guard_rejects_control_chars_and_non_loopback_ips(host):
    # Raw ASGI scope: httpx won't emit some of these, but a hostile client can.
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():  # pragma: no cover — never read
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "method": "GET", "path": "/mcp", "headers": [(b"host", host)]}
    await HostOriginGuard(_ok_app)(scope, receive, send)
    assert sent[0]["status"] == 421


async def test_guard_rejects_origin_with_control_chars():
    async with _guarded_client("http://localhost:8000") as client:
        resp = await client.get("/mcp", headers={"Origin": "http://local\x7fhost"})
    assert resp.status_code == 403


async def test_guard_allows_operator_listed_hosts(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "box.tailnet.ts.net, Proxy.Internal:8443")
    hosts = parse_allowed_hosts_env()
    assert hosts == frozenset({"box.tailnet.ts.net", "proxy.internal"})
    async with _guarded_client("http://box.tailnet.ts.net:8000", allowed_hosts=hosts) as client:
        resp = await client.get("/mcp")
    assert resp.status_code == 200


# ── cli.main wiring ───────────────────────────────────────────────────────


@pytest.fixture
def kalshi_env(monkeypatch, tmp_path, rsa_private_key):
    pem = rsa_private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    monkeypatch.chdir(tmp_path)  # no stray .env gets loaded
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PEM", pem)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setenv("KALSHI_ENV", "demo")
    # main() reconfigures the root logger; restore it so later tests don't
    # inherit a handler bound to this test's captured stderr.
    root = logging.getLogger()
    saved = (root.level, list(root.handlers))
    yield
    root.handlers[:] = saved[1]
    root.setLevel(saved[0])


@pytest.fixture
def captured_run(monkeypatch):
    """Replace FastMCP.run so main() returns instead of serving."""
    calls: list[tuple[FastMCP, dict]] = []

    def fake_run(self, *args, **kwargs):
        calls.append((self, kwargs))

    monkeypatch.setattr(FastMCP, "run", fake_run)
    return calls


def test_main_reports_http_policy_refusal_without_traceback(kalshi_env, captured_run, capsys):
    assert main(["--transport", "http"]) == 2
    err = capsys.readouterr().err
    assert "Startup failed: HTTP transport requires OAuth" in err
    assert "Traceback" not in err
    assert captured_run == []


def test_main_reports_partial_oauth_without_traceback(
    kalshi_env, captured_run, capsys, monkeypatch
):
    monkeypatch.setenv("GITHUB_CLIENT_ID", "id")
    assert main(["--transport", "http"]) == 2
    err = capsys.readouterr().err
    assert "Startup failed: Partial OAuth configuration" in err
    assert "Traceback" not in err
    assert captured_run == []


def test_main_stdio_only_warns_on_partial_oauth(kalshi_env, captured_run, monkeypatch, capsys):
    # GITHUB_CLIENT_ID is a generic name and ./.env is auto-loaded; stdio
    # never uses OAuth, so an unrelated value must not stop it. (main()
    # re-runs logging.basicConfig(force=True), so read stderr, not caplog.)
    monkeypatch.setenv("GITHUB_CLIENT_ID", "some-other-app")
    assert main(["--transport", "stdio"]) == 0
    assert "Partial OAuth configuration" in capsys.readouterr().err
    (server, kwargs) = captured_run[0]
    assert kwargs["transport"] == "stdio"
    assert server.auth is None


def test_main_http_oauth_with_leftover_insecure_flag_boots_with_oauth(
    kalshi_env, captured_run, monkeypatch
):
    _set_oauth(monkeypatch)
    monkeypatch.setenv("MCP_ALLOWED_GITHUB_IDS", "4242")
    monkeypatch.setenv("MCP_ALLOW_INSECURE_HTTP", "1")
    monkeypatch.setenv("MCP_ALLOW_INSECURE_PUBLIC_BIND", "1")
    assert main(["--transport", "http", "--host", "0.0.0.0"]) == 0  # noqa: S104
    (server, kwargs) = captured_run[0]
    assert server.auth is not None
    assert any(isinstance(m, RestrictGitHubUsersMiddleware) for m in server.middleware)
    assert kwargs.get("middleware") is None


def test_main_refuses_insecure_public_bind(kalshi_env, captured_run, capsys, monkeypatch):
    monkeypatch.setenv("MCP_ALLOW_INSECURE_HTTP", "1")
    assert main(["--transport", "http", "--host", "0.0.0.0"]) == 2  # noqa: S104
    assert "MCP_ALLOW_INSECURE_PUBLIC_BIND" in capsys.readouterr().err
    assert captured_run == []


def test_main_stdio_does_not_install_allowlist(kalshi_env, captured_run, monkeypatch):
    # The env file carries a full OAuth + allowlist config (shared with an
    # HTTP deploy). On stdio there is no access token, so installing the
    # allowlist would refuse every local request.
    _set_oauth(monkeypatch)
    monkeypatch.setenv("MCP_ALLOWED_GITHUB_LOGINS", "alice")
    assert main(["--transport", "stdio"]) == 0
    (server, kwargs) = captured_run[0]
    assert kwargs["transport"] == "stdio"
    assert not any(isinstance(m, RestrictGitHubUsersMiddleware) for m in server.middleware)


def test_main_http_oauth_installs_allowlist_and_no_host_guard(
    kalshi_env, captured_run, monkeypatch
):
    _set_oauth(monkeypatch)
    monkeypatch.setenv("MCP_ALLOWED_GITHUB_IDS", "4242")
    assert main(["--transport", "http", "--host", "0.0.0.0"]) == 0  # noqa: S104
    (server, kwargs) = captured_run[0]
    assert any(isinstance(m, RestrictGitHubUsersMiddleware) for m in server.middleware)
    assert kwargs.get("middleware") is None


def test_main_insecure_http_installs_host_guard(kalshi_env, captured_run, monkeypatch):
    monkeypatch.setenv("MCP_ALLOW_INSECURE_HTTP", "1")
    assert main(["--transport", "http"]) == 0
    (server, kwargs) = captured_run[0]
    assert [m.cls for m in kwargs["middleware"]] == [HostOriginGuard]
    assert not any(isinstance(m, RestrictGitHubUsersMiddleware) for m in server.middleware)


# ── real HTTP transport (no get_access_token patching) ────────────────────
#
# Everything above patches `get_access_token`. This drives the real
# streamable-HTTP app — bearer auth middleware, token verifier, FastMCP's
# request routing — in-process over httpx.ASGITransport (no socket, no
# network), with StaticTokenVerifier standing in for the GitHub verifier
# (it puts the same `sub` / `login` claims on the AccessToken).


def _real_http_server() -> FastMCP:
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    verifier = StaticTokenVerifier(
        tokens={
            "insider-token": {"client_id": "c", "scopes": [], "sub": "4242", "login": "alice"},
            "outsider-token": {"client_id": "c", "scopes": [], "sub": "666", "login": "mallory"},
        }
    )
    server = FastMCP("perimeter-e2e", auth=verifier)

    @server.resource("kalshi://balance")
    def balance() -> str:
        return '{"balance": 100}'

    server.add_middleware(RestrictGitHubUsersMiddleware(frozenset(), frozenset({"4242"})))
    return server


async def _rpc(app, token: str, method: str, params: dict) -> dict:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        resp = await http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
        )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_real_transport_refuses_outsider_resource_read():
    # Stateless + JSON responses so a bare resources/read is a complete
    # request/response, letting us hit that method directly.
    app = _real_http_server().http_app(stateless_http=True, json_response=True)
    async with app.router.lifespan_context(app):
        denied = await _rpc(app, "outsider-token", "resources/read", {"uri": "kalshi://balance"})
        allowed = await _rpc(app, "insider-token", "resources/read", {"uri": "kalshi://balance"})

    assert denied["error"]["code"] == oauth.FORBIDDEN_ERROR_CODE
    assert "mallory" in denied["error"]["message"]
    assert "100" not in str(denied)
    assert "100" in allowed["result"]["contents"][0]["text"]


async def test_real_transport_client_session():
    from fastmcp.client.transports import StreamableHttpTransport

    app = _real_http_server().http_app()

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), **kwargs)

    def transport(token: str) -> StreamableHttpTransport:
        return StreamableHttpTransport(
            "http://localhost/mcp", auth=token, httpx_client_factory=factory
        )

    async with app.router.lifespan_context(app):
        async with Client(transport("insider-token")) as client:
            contents = await client.read_resource("kalshi://balance")
            assert "100" in contents[0].text

        with pytest.raises(McpError) as exc:
            async with Client(transport("outsider-token")):
                pass
        assert exc.value.error.code == oauth.FORBIDDEN_ERROR_CODE
