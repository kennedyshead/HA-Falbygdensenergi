#!/usr/bin/env python3
"""Map the authenticated API of minasidor.falbygdensenergi.se.

The consumption / meter endpoints are only visible after login, so run this
once with your own credentials.  It writes ``tools/out/`` with:

* ``discovery.json`` – every page reachable from the menu and the API
  endpoints its inline JavaScript calls
* ``pages/<page>.html`` – the raw page HTML (inline scripts included)
* ``api/<sanitised-path>.json`` – the response of every parameter-less GET
  endpoint that was found

Nothing is sent anywhere except to the portal itself.  Review the output
before sharing it: the HTML pages contain your name, address and customer
number.

Usage::

    .venv/bin/python tools/discover.py             # uses FBE_USERNAME/FBE_PASSWORD from .envrc
    .venv/bin/python tools/discover.py USERNAME            # prompts for password
    PORTAL_PASSWORD=... .venv/bin/python tools/discover.py USERNAME

``.envrc`` is parsed literally (no shell expansion), so passwords containing
``$`` or ``#`` survive even inside double quotes.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.falbygdens_energi.api import (  # noqa: E402
    FalbygdensEnergiClient,
    FalbygdensEnergiError,
)

OUT = ROOT / "tools" / "out"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")[:120] or "index"


async def main(username: str, password: str) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "pages").mkdir(exist_ok=True)
    (OUT / "api").mkdir(exist_ok=True)

    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        client = FalbygdensEnergiClient(session, username, password)
        try:
            info = await client.async_login()
        except FalbygdensEnergiError as err:
            print(f"Login failed: {err}", file=sys.stderr)
            return 1
        print(f"Logged in. customer_id={info.customer_id} customer_code={info.customer_code}")
        print(f"meta: {json.dumps(info.raw_meta, ensure_ascii=False)}")

        result = await client.async_discover()
        (OUT / "discovery.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))

        # Save every page's HTML so the JavaScript can be read offline.
        for page in result["pages"]:
            try:
                html = await client.async_get_page(page)
            except FalbygdensEnergiError as err:
                print(f"  ! {page}: {err}")
                continue
            (OUT / "pages" / f"{_safe(page)}.html").write_text(html)

        # Try every endpoint that has no path parameters, GET only.
        endpoints = sorted(
            {
                e
                for p in result["pages"].values()
                for e in p.get("endpoints", [])
                if "${" not in e and "{" not in e and ".aspx/" not in e
            }
        )
        print(f"\n{len(endpoints)} parameter-less endpoints found:")
        for ep in endpoints:
            path = ep.lstrip("/")
            try:
                data = await client.async_request("GET", path)
                status = "ok"
            except FalbygdensEnergiError as err:
                data = {"error": str(err)}
                status = "ERR"
            (OUT / "api" / f"{_safe(path)}.json").write_text(
                json.dumps(data, indent=2, ensure_ascii=False, default=str)
            )
            preview = json.dumps(data, ensure_ascii=False, default=str)
            print(f"  [{status}] {path}  ->  {preview[:100]}")

        print("\nPages and the endpoints their scripts call:")
        for page, meta in result["pages"].items():
            print(f"\n  {page}")
            for ep in meta.get("endpoints", []):
                print(f"      {ep}")
    print(f"\nWrote {OUT}")
    return 0


def _read_envrc(path: Path) -> dict[str, str]:
    """Parse ``export KEY=VALUE`` lines literally, stripping one layer of quotes."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for key, value in re.findall(r"^\s*(?:export\s+)?(\w+)=(.*)$", path.read_text(), re.M):
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


if __name__ == "__main__":
    if len(sys.argv) > 2:
        print(__doc__)
        sys.exit(2)
    envrc = _read_envrc(ROOT / ".envrc")
    user = sys.argv[1] if len(sys.argv) == 2 else envrc.get("FBE_USERNAME")
    if not user:
        print(__doc__)
        sys.exit(2)
    pw = (
        os.environ.get("PORTAL_PASSWORD")
        or envrc.get("FBE_PASSWORD")
        or getpass.getpass("Portal password: ")
    )
    sys.exit(asyncio.run(main(user, pw)))
