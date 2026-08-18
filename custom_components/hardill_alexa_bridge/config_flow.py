"""Config flow for Hardill Alexa Bridge."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .api import HardillCannotConnect, HardillInvalidAuth, async_fetch_devices
from .const import CONF_PASSWORD, CONF_USERNAME, DOMAIN


class HardillAlexaBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hardill Alexa Bridge."""

    VERSION = 2

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Authenticate the Hardill account."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]

            try:
                # The list may legitimately be empty: v0.2 can create devices itself.
                await async_fetch_devices(self.hass, username, password)
            except HardillInvalidAuth:
                errors["base"] = "invalid_auth"
            except HardillCannotConnect:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(username)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Hardill Alexa ({username})",
                    data={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )
