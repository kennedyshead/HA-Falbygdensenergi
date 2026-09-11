"""End-to-end test against the real portal.

Runs only when ``.envrc`` with ``FBE_USERNAME`` / ``FBE_PASSWORD`` exists (it is
git-ignored), so CI and other machines skip it automatically.
"""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.falbygdens_energi.const import DOMAIN
from tools.discover import _read_envrc

ENV = _read_envrc(Path(__file__).resolve().parents[1] / ".envrc")

pytestmark = pytest.mark.skipif(
    not (ENV.get("FBE_USERNAME") and ENV.get("FBE_PASSWORD")),
    reason="no .envrc with portal credentials",
)


async def test_live_portal(recorder_mock, hass: HomeAssistant, socket_enabled) -> None:
    await hass.config.async_set_time_zone("Europe/Stockholm")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ENV["FBE_USERNAME"].lower(),
        title="Falbygdens Energi (live)",
        data={CONF_USERNAME: ENV["FBE_USERNAME"], CONF_PASSWORD: ENV["FBE_PASSWORD"]},
    )
    entry.add_to_hass(hass)
    # The test harness disables DNS for non-IP hosts and mocks HA's async
    # resolver.  For this live run, give the integration a plain session with a
    # thread resolver and restore the real getaddrinfo while it talks to the portal.
    import pytest_socket  # noqa: PLC0415
    from pytest_homeassistant_custom_component import plugins  # noqa: PLC0415

    from custom_components.falbygdens_energi.const import BASE_URL  # noqa: PLC0415

    host = BASE_URL.split("//", 1)[1].split("/", 1)[0]
    portal_ips = sorted({info[4][0] for info in plugins._real_getaddrinfo(host, 443)})
    pytest_socket.socket_allow_hosts(["127.0.0.1", *portal_ips], allow_unix_socket=True)

    sessions: list[aiohttp.ClientSession] = []

    def _plain_session(_hass: HomeAssistant, **kwargs) -> aiohttp.ClientSession:
        connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
        session = aiohttp.ClientSession(connector=connector, **kwargs)
        sessions.append(session)
        return session

    with (
        patch.object(socket, "getaddrinfo", plugins._real_getaddrinfo),
        patch("custom_components.falbygdens_energi.async_create_clientsession", _plain_session),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED, entry.reason

    data = entry.runtime_data.data
    assert data.sites, "no sites found on the account"
    site = data.sites[0]
    print(f"\nsite: {site.site.name} meter={site.primary_meter.meter_id}")
    print(f"today={site.today} yesterday={site.yesterday} month={site.month_to_date}")
    print(f"year={site.year_to_date} last_year={site.last_year_to_date}")
    print(f"meter_stand={site.meter_stand} @ {site.meter_stand_date}")
    print(f"data_up_to={site.last_hour_start}")
    inv = data.invoices
    print(
        f"invoices: unpaid={inv.unpaid_amount} latest={inv.latest.amount} due={inv.next_due_date}"
    )

    assert site.yesterday and site.yesterday > 0
    assert site.month_to_date and site.month_to_date > 0
    assert site.year_to_date and site.year_to_date > 0
    assert site.meter_stand and site.meter_stand > 0
    assert site.last_hour_start is not None

    states = {
        s.entity_id: s.state for s in hass.states.async_all("sensor") if s.state != "unavailable"
    }
    for entity_id, state in sorted(states.items()):
        print(f"  {entity_id} = {state}")
    assert len(states) >= 10

    await async_wait_recording_done(hass)
    statistic_id = f"{DOMAIN}:{site.primary_meter.meter_id}_energy"
    stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum", "state"}
    )
    rows = stats[statistic_id]
    print(f"statistics {statistic_id}: last row {rows[0]}")
    assert rows[0]["sum"] > 0

    assert await hass.config_entries.async_unload(entry.entry_id)
    for session in sessions:
        await session.close()
