"""Tests for the config flow."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.falbygdens_energi.api import (
    AuthenticationError,
    CannotConnectError,
    PortalInfo,
    TwoFactorRequiredError,
)
from custom_components.falbygdens_energi.const import DOMAIN

VALIDATE = "custom_components.falbygdens_energi.config_flow.validate_credentials"


async def test_user_flow_creates_entry(recorder_mock, hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    info = PortalInfo(customer_id="12345", customer_code="ABC123")
    with (
        patch(VALIDATE, return_value=info),
        patch("custom_components.falbygdens_energi.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: " user ", CONF_PASSWORD: "secret"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Falbygdens Energi (ABC123)"
    assert result["data"][CONF_USERNAME] == "user"
    assert result["data"]["customer_id"] == "12345"
    assert result["result"].unique_id == "user"


async def test_user_flow_errors(recorder_mock, hass: HomeAssistant) -> None:
    for i, (exc, key) in enumerate(
        [
            (AuthenticationError("x"), "invalid_auth"),
            (TwoFactorRequiredError("x"), "two_factor"),
            (CannotConnectError("x"), "cannot_connect"),
            (RuntimeError("x"), "unknown"),
        ]
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        with patch(VALIDATE, side_effect=exc):
            # Distinct user names: a second flow for the same unique_id would
            # abort with "already_in_progress".
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_USERNAME: f"user{i}", CONF_PASSWORD: "x"}
            )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": key}


async def test_duplicate_account_aborts(recorder_mock, hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain=DOMAIN, unique_id="user", data={CONF_USERNAME: "user", CONF_PASSWORD: "x"}
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "USER", CONF_PASSWORD: "x"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password(recorder_mock, hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="user", data={CONF_USERNAME: "user", CONF_PASSWORD: "old"}
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    with (
        patch(VALIDATE, return_value=PortalInfo()),
        patch("custom_components.falbygdens_energi.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new"
