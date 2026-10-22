"""Config flow for Keurig Connect integration."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from .const import DOMAIN
from .pykeurig.keurigapi import KeurigApi

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class KeurigConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Keurig Connect."""

    VERSION = 1

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self._brewers: dict[str, str] | None = None

    async def _async_validate(self, username: str, password: str) -> dict[str, str]:
        """Log in and return the account's brewers. Raises on failure."""
        api = KeurigApi()
        if not await api.login(username, password):
            raise PermissionError("invalid_auth")
        return {dev.id: dev.name for dev in await api.async_get_devices()}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_USERNAME].strip().lower())
            self._abort_if_unique_id_configured()
            try:
                self._brewers = await self._async_validate(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except PermissionError:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception during Keurig login")
                errors["base"] = "unknown"
            else:
                self.data = user_input
                return await self.async_step_devices()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle brewer selection step."""
        if user_input is None:
            return self.async_show_form(
                step_id="devices",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            "brewers", default=list(self._brewers or {})
                        ): cv.multi_select(self._brewers or {})
                    }
                ),
            )
        self.data.update(user_input)
        return self.async_create_entry(title=self.data[CONF_USERNAME], data=self.data)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication when the Keurig login expires."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again and update the existing entry."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                await self._async_validate(
                    entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except PermissionError:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception during Keurig re-login")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )
