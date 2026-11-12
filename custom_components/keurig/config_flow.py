"""Config flow for Keurig Connect integration.

Keurig retired password login, so sign-in is done once in a browser
(Auth0 authorization code  PKCE). The user pastes back the URL the browser
landed on; after that the integration runs on rotating refresh tokens.
"""
from __future__ import annotations

import logging
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from .pykeurig.keurigapi import KeurigApi, UnauthorizedException

from .const import CONF_REFRESH_TOKEN, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_REDIRECT_URL = "redirect_url"
SIGN_IN_SCHEMA = vol.Schema({vol.Required(CONF_REDIRECT_URL): str})


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Keurig Connect."""

    VERSION = 1

    def __init__(self):
        self._api = KeurigApi()
        self._auth_request: dict | None = None
        self._claims: dict = {}
        self._brewers: dict = {}

    def _account_id(self) -> str | None:
        """Unique ID: the lowercased account email, as earlier versions used."""
        email = self._claims.get("email")
        return email.strip().lower() if email else None

    def _auth_url(self) -> str:
        """Create the PKCE sign-in request once per flow."""
        if self._auth_request is None:
            self._auth_request = KeurigApi.create_authorization_request()
        return self._auth_request["url"]

    async def _try_sign_in(self, redirect_url: str) -> str | None:
        """Exchange the pasted URL for tokens; return an error key or None."""
        try:
            self._claims = await self._api.async_complete_authorization(
                redirect_url,
                self._auth_request["code_verifier"],
                self._auth_request["state"],
            )
        except ValueError as err:
            _LOGGER.warning("Keurig sign-in URL not accepted: %s", err)
            return "invalid_redirect_url"
        except UnauthorizedException as err:
            # Codes are single-use and short-lived: start a fresh sign-in
            _LOGGER.warning("Keurig rejected the sign-in code: %s", err)
            self._auth_request = None
            return "invalid_auth"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Keurig sign-in failed")
            return "cannot_connect"
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in via the browser, then pick brewers."""
        errors = {}
        if user_input is not None:
            error = await self._try_sign_in(user_input[CONF_REDIRECT_URL])
            if error:
                errors["base"] = error
            else:
                if email := self._account_id():
                    await self.async_set_unique_id(email)
                    self._abort_if_unique_id_configured()
                try:
                    self._brewers = {
                        dev.id: dev.name for dev in await self._api.async_get_devices()
                    }
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("Failed to list Keurig brewers")
                    errors["base"] = "unknown"
                else:
                    return await self.async_step_devices()

        return self.async_show_form(
            step_id="user",
            data_schema=SIGN_IN_SCHEMA,
            description_placeholders={"url": self._auth_url()},
            errors=errors,
        )

    async def async_step_devices(self, user_input: dict[str, Any] | None = None):
        """Handle brewer selection step."""
        if user_input is None:
            return self.async_show_form(
                step_id="devices",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            "brewers", default=list(self._brewers)
                        ): cv.multi_select(self._brewers)
                    }
                ),
            )
        email = self._claims.get("email", "Keurig")
        return self.async_create_entry(
            title=email,
            data={
                CONF_USERNAME: email,
                CONF_REFRESH_TOKEN: self._api.refresh_token,
                "brewers": user_input["brewers"],
            },
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauth when the refresh token stops working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in again via the browser and update the existing entry."""
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            error = await self._try_sign_in(user_input[CONF_REDIRECT_URL])
            if error:
                errors["base"] = error
            else:
                account = self._account_id()
                if entry.unique_id and account and entry.unique_id != account:
                    return self.async_abort(reason="wrong_account")
                data = {
                    k: v for k, v in entry.data.items() if k != CONF_PASSWORD
                }  # drop the password stored by the old login
                data[CONF_REFRESH_TOKEN] = self._api.refresh_token
                return self.async_update_reload_and_abort(
                    entry, unique_id=account or entry.unique_id, data=data
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=SIGN_IN_SCHEMA,
            description_placeholders={
                "url": self._auth_url(),
                "username": entry.data.get(CONF_USERNAME, entry.title),
            },
            errors=errors,
        )
