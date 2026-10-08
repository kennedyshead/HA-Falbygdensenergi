"""Tests for the portal client against a fake portal."""

from __future__ import annotations

import aiohttp
import pytest
from yarl import URL

from custom_components.falbygdens_energi.api import (
    AccountLockedError,
    AuthenticationError,
    CannotConnectError,
    CaptchaRequiredError,
    FalbygdensEnergiClient,
    TwoFactorRequiredError,
)


def _client(
    base: str, user: str = "user", pw: str = "secret"
) -> tuple[aiohttp.ClientSession, FalbygdensEnergiClient]:
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    return session, FalbygdensEnergiClient(session, user, pw, base_url=base)


async def test_login_success_reads_meta(portal) -> None:
    fake, base = portal
    session, client = _client(base)
    async with session:
        info = await client.async_login()
        assert info.customer_id == "12345"
        assert info.customer_code == "ABC123"
        assert info.is_private_person is True
        assert info.portal_version == "13.0.26138.52557"
        assert client.is_logged_in
        assert fake.calls[:2] == ["default", "authenticate"]


async def test_login_wrong_password(portal) -> None:
    _, base = portal
    session, client = _client(base, pw="nope")
    async with session:
        with pytest.raises(AuthenticationError):
            await client.async_login()
        assert not client.is_logged_in


@pytest.mark.parametrize(
    "landing",
    [
        "~/start.aspx?return=a%2Fb&lang=sv",
        "/start.aspx?return=a%2Fb&lang=sv",
        "start.aspx?return=a%2Fb&lang=sv",
        "https://127.0.0.1:444/start.aspx?return=a%2Fb&lang=sv",
        "http://127.0.0.1:444/start.aspx?return=a%2Fb&lang=sv",
        "//127.0.0.1:444/start.aspx?return=a%2Fb&lang=sv",
    ],
)
async def test_login_keeps_landing_on_configured_origin(portal, landing) -> None:
    fake, base = portal
    fake.landing_url = landing
    session, client = _client(base)
    async with session:
        info = await client.async_login()
        assert info.customer_id == "12345"
        assert info.landing_url == str(URL(base).join(URL("/start.aspx?return=a%2Fb&lang=sv")))
        assert URL(info.landing_url).origin() == URL(base).origin()
        assert client.is_logged_in
        assert await client.async_request("GET", "api/consumption/meters") == [{"MeterId": "1"}]


@pytest.mark.parametrize(
    ("landing", "message"),
    [
        ("https://other.example/start.aspx", "different host"),
        ("//other.example/start.aspx", "different host"),
        ("ftp://127.0.0.1/start.aspx", "non-HTTP"),
        ("https://127.0.0.1:invalid/start.aspx", "invalid landing URL"),
    ],
)
async def test_login_rejects_unsafe_landing_url(portal, landing, message) -> None:
    fake, base = portal
    fake.landing_url = landing
    session, client = _client(base)
    async with session:
        with pytest.raises(CannotConnectError, match=message):
            await client.async_login()
        assert not client.is_logged_in


@pytest.mark.parametrize(
    ("status", "exc"),
    [(3, TwoFactorRequiredError), (7, AccountLockedError), (2, CaptchaRequiredError)],
)
async def test_login_special_statuses(portal, status, exc) -> None:
    fake, base = portal
    fake.login_status = status
    session, client = _client(base)
    async with session:
        with pytest.raises(exc):
            await client.async_login()


async def test_login_refuses_when_captcha_active(portal) -> None:
    fake, base = portal
    fake.captcha_active = True
    session, client = _client(base)
    async with session:
        with pytest.raises(CaptchaRequiredError):
            await client.async_login()
        assert "authenticate" not in fake.calls


async def test_request_relogins_after_401(portal) -> None:
    fake, base = portal
    session, client = _client(base)
    async with session:
        await client.async_login()
        # Simulate the server-side session dying.
        fake.authenticated = False
        data = await client.async_request("GET", "api/consumption/meters")
        assert data == [{"MeterId": "1"}]
        assert fake.calls.count("authenticate") == 2


async def test_discover_lists_endpoints(portal) -> None:
    _, base = portal
    session, client = _client(base)
    async with session:
        await client.async_login()
        result = await client.async_discover()
        assert "start.aspx" in result["pages"]
        assert "Consumption.aspx" in result["pages"]
        assert result["pages"]["start.aspx"]["endpoints"] == ["api/consumption/meters"]
