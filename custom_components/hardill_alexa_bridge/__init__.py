"""Hardill Alexa Bridge integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .bridge import HardillAlexaBridge
from .const import CONF_MAPPINGS, CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .sync import HardillExposureSync


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hardill Alexa Bridge from a config entry."""
    legacy_mappings = entry.options.get(
        CONF_MAPPINGS,
        entry.data.get(CONF_MAPPINGS, {}),
    )

    bridge = HardillAlexaBridge(
        hass=hass,
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        mappings=legacy_mappings,
    )
    sync = HardillExposureSync(
        hass=hass,
        entry_id=entry.entry_id,
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        bridge=bridge,
        legacy_mappings=legacy_mappings,
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "bridge": bridge,
        "sync": sync,
    }
    # Build the appliance-to-entity map before subscribing to MQTT so an Alexa
    # command cannot arrive during startup while mappings are still empty.
    await sync.async_start()
    await bridge.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Hardill Alexa Bridge config entry."""
    runtime = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if runtime is not None:
        await runtime["sync"].async_stop()
        await runtime["bridge"].async_stop()
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate pre-auto-sync config entries."""
    if entry.version < 2:
        hass.config_entries.async_update_entry(entry, version=2)
    return True
