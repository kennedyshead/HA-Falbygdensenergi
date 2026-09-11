"""The Falbygdens Energi integration."""

from __future__ import annotations

import logging

import aiohttp
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import AuthenticationError, CannotConnectError, FalbygdensEnergiClient
from .const import BASE_URL
from .coordinator import FalbygdensEnergiConfigEntry, FalbygdensEnergiCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: FalbygdensEnergiConfigEntry) -> bool:
    """Set up Falbygdens Energi from a config entry."""
    # Dedicated session so the portal's auth cookies never mix with other
    # integrations talking to the same host through the shared session.
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    client = FalbygdensEnergiClient(
        session, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD], base_url=BASE_URL
    )
    try:
        await client.async_login()
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except CannotConnectError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = FalbygdensEnergiCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FalbygdensEnergiConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
