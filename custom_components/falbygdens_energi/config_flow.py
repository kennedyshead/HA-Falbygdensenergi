"""Config flow for the Falbygdens Energi integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import (
    AccountLockedError,
    AuthenticationError,
    CannotConnectError,
    CaptchaRequiredError,
    FalbygdensEnergiClient,
    PasswordLoginDisabledError,
    PortalInfo,
    TwoFactorRequiredError,
)
from .const import CONF_CUSTOMER_CODE, CONF_CUSTOMER_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)
STEP_REAUTH_DATA_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


async def validate_credentials(hass: HomeAssistant, username: str, password: str) -> PortalInfo:
    """Try to log in and return the portal info.  Raises client errors on failure."""
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    try:
        client = FalbygdensEnergiClient(session, username, password)
        return await client.async_login()
    finally:
        await session.close()


def _error_key(err: Exception) -> str:
    if isinstance(err, TwoFactorRequiredError):
        return "two_factor"
    if isinstance(err, AccountLockedError):
        return "account_locked"
    if isinstance(err, CaptchaRequiredError):
        return "captcha"
    if isinstance(err, PasswordLoginDisabledError):
        return "password_login_disabled"
    if isinstance(err, AuthenticationError):
        return "invalid_auth"
    if isinstance(err, CannotConnectError):
        return "cannot_connect"
    return "unknown"


class FalbygdensEnergiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Falbygdens Energi."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()
            try:
                info = await validate_credentials(self.hass, username, user_input[CONF_PASSWORD])
            except Exception as err:  # noqa: BLE001 - map every failure to a form error
                errors["base"] = _error_key(err)
                if errors["base"] == "unknown":
                    _LOGGER.exception("Unexpected error validating credentials")
            else:
                return self.async_create_entry(
                    title=f"Falbygdens Energi ({info.customer_code or username})",
                    data={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_CUSTOMER_ID: info.customer_id,
                        CONF_CUSTOMER_CODE: info.customer_code,
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Handle re-authentication when the password stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new password."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                await validate_credentials(
                    self.hass, entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except Exception as err:  # noqa: BLE001
                errors["base"] = _error_key(err)
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            description_placeholders={CONF_USERNAME: entry.data[CONF_USERNAME]},
            errors=errors,
        )
