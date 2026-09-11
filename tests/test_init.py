"""Tests for entry setup against the fake portal."""

from __future__ import annotations

from datetime import UTC, datetime
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
from tests.conftest import FAKE_NOW


async def _setup(hass: HomeAssistant, base: str, password: str = "secret") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user",
        title="Falbygdens Energi (ABC123)",
        data={CONF_USERNAME: "user", CONF_PASSWORD: password},
    )
    entry.add_to_hass(hass)
    # The portal reports local (Swedish) time; the default test zone is US/Pacific.
    await hass.config.async_set_time_zone("Europe/Stockholm")
    # The fake portal lives on 127.0.0.1 and aiohttp refuses cookies from bare
    # IPs unless the jar is "unsafe"; the real portal is a host name.
    real_jar = aiohttp.CookieJar
    with (
        patch("custom_components.falbygdens_energi.BASE_URL", base),
        patch(
            "custom_components.falbygdens_energi.aiohttp.CookieJar",
            lambda **kw: real_jar(unsafe=True),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.fixture(autouse=True)
def _freeze_now(freezer) -> None:
    """Pin "now" to the fake portal's clock."""
    freezer.move_to(FAKE_NOW)


async def test_setup_creates_sensors(recorder_mock, hass: HomeAssistant, portal) -> None:
    fake, base = portal
    entry = await _setup(hass, base)

    assert entry.state is ConfigEntryState.LOADED
    assert "version" in fake.calls
    assert fake.calls.count("onload") == 1
    # First run backfills 30 days of hourly data plus the monthly series.
    hourly = [m for m in fake.consumption_requests if m["Interval"] == "HOUR"]
    assert len(hourly) == 1
    assert hourly[0]["StartDate"] == "2026-08-12"
    assert hourly[0]["EndDate"] == "2026-09-11"
    assert hourly[0]["CompareType"] == "none"

    # Account sensors
    state = hass.states.get("sensor.falbygdens_energi_abc123_last_update")
    assert state is not None, hass.states.async_entity_ids("sensor")
    assert state.attributes["customer_code"] == "ABC123"
    unpaid = hass.states.get("sensor.falbygdens_energi_abc123_unpaid_invoices")
    assert unpaid.state == "1790.0"
    assert unpaid.attributes["unpaid_count"] == 1
    assert hass.states.get("sensor.falbygdens_energi_abc123_latest_invoice").state == "1790.0"
    assert hass.states.get("sensor.falbygdens_energi_abc123_next_due_date").state == "2026-09-30"

    # Site sensors (device named after the address)
    site = entry.runtime_data.data.sites[0]
    assert site.site.name == "Testgatan 1, TESTSTAD"
    assert site.primary_meter.meter_id == "55782955"
    meter = hass.states.get("sensor.testgatan_1_teststad_meter_reading")
    assert meter.state == "116041.594"
    assert meter.attributes["reading_date"].startswith("2026-09-01")
    assert meter.attributes["facility_id"] == "735999151202487543"

    # Hours are 1.00 + hour/100; today (local, Europe/Stockholm = UTC+2) covers
    # 00:00–23:00 local, but the fake stops delivering 2 h before FAKE_NOW.
    today = hass.states.get("sensor.testgatan_1_teststad_energy_today")
    assert today.attributes["state_class"] == "total"
    assert 0 < float(today.state) < 24
    yesterday = hass.states.get("sensor.testgatan_1_teststad_energy_yesterday")
    assert float(yesterday.state) == pytest.approx(sum(1.0 + h / 100 for h in range(24)), abs=0.01)
    assert hass.states.get("sensor.testgatan_1_teststad_energy_this_month").state == "1009.0"
    assert hass.states.get("sensor.testgatan_1_teststad_energy_this_year").state == "9009.0"
    up_to = hass.states.get("sensor.testgatan_1_teststad_data_up_to")
    assert up_to.state == "2026-09-11T07:00:00+00:00"

    # Long-term statistics were imported under the meter id.
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, "falbygdens_energi:55782955_energy", True, {"sum", "state"}
    )
    rows = stats["falbygdens_energi:55782955_energy"]
    assert rows[0]["start"] == datetime(2026, 9, 11, 7, tzinfo=UTC).timestamp()
    assert rows[0]["sum"] > 24 * 29  # roughly 30 days of ~1 kWh/h

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_refresh_reuses_sum_and_short_window(
    recorder_mock, hass: HomeAssistant, portal
) -> None:
    fake, base = portal
    entry = await _setup(hass, base)
    await async_wait_recording_done(hass)
    first = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, "falbygdens_energi:55782955_energy", True, {"sum"}
    )
    first_sum = first["falbygdens_energi:55782955_energy"][0]["sum"]

    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    hourly = [m for m in fake.consumption_requests if m["Interval"] == "HOUR"]
    assert len(hourly) == 2
    assert hourly[1]["StartDate"] == "2026-09-08"  # 3-day refresh window
    second = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, "falbygdens_energi:55782955_energy", True, {"sum"}
    )
    # Re-importing the same hours must not double count.
    assert second["falbygdens_energi:55782955_energy"][0]["sum"] == pytest.approx(first_sum)


async def test_setup_bad_password_triggers_reauth(
    recorder_mock, hass: HomeAssistant, portal
) -> None:
    _, base = portal
    entry = await _setup(hass, base, password="wrong")

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"].get("source") == "reauth" for f in flows)
