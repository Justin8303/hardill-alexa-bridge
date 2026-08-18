"""Synchronize Home Assistant Assist exposure with Hardill Alexa devices."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.homeassistant.exposed_entities import (
    async_listen_entity_updates,
    async_should_expose,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, State, callback
from homeassistant.helpers.storage import Store

from .api import (
    HardillCannotConnect,
    HardillDevice,
    HardillInvalidAuth,
    HardillWebSession,
    async_fetch_devices,
)
from .bridge import HardillAlexaBridge
from .const import (
    EXPOSURE_ASSISTANT,
    MANAGED_DESCRIPTION_PREFIX,
    STORE_KEY_PREFIX,
    STORE_VERSION,
    SYNC_DEBOUNCE_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeviceSpec:
    """Hardill capabilities derived from one Home Assistant entity."""

    entity_id: str
    name: str
    description: str
    actions: tuple[str, ...]
    appliance_types: tuple[str, ...]


class HardillExposureSync:
    """Keep the selected Home Assistant voice entities in Hardill's bridge."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        username: str,
        password: str,
        bridge: HardillAlexaBridge,
        legacy_mappings: dict[str, str] | None = None,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.username = username
        self.password = password
        self.bridge = bridge
        self.legacy_mappings = {
            str(key): value
            for key, value in (legacy_mappings or {}).items()
            if value
        }
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{STORE_KEY_PREFIX}.{entry_id}"
        )
        self._managed: dict[str, dict[str, Any]] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        self._sync_task: asyncio.Task[None] | None = None
        self._debounce_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def async_start(self) -> None:
        """Load state, subscribe for expose changes and perform initial sync."""
        stored = await self._store.async_load() or {}
        managed = stored.get("managed", {})
        if isinstance(managed, dict):
            self._managed = {
                str(entity_id): record
                for entity_id, record in managed.items()
                if isinstance(record, dict)
            }

        self._unsubs.append(
            async_listen_entity_updates(
                self.hass, EXPOSURE_ASSISTANT, self._schedule_sync
            )
        )
        # Renames/removals in the entity registry may affect the Alexa name or
        # whether an entity still exists. Exposure changes have their own listener.
        self._unsubs.append(
            self.hass.bus.async_listen("entity_registry_updated", self._registry_updated)
        )
        await self.async_sync()

    async def async_stop(self) -> None:
        """Stop listeners and pending synchronization work."""
        self._stopping = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for task in (self._debounce_task, self._sync_task):
            if task is not None:
                task.cancel()
        for task in (self._debounce_task, self._sync_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._debounce_task = None
        self._sync_task = None

    @callback
    def _registry_updated(self, _event) -> None:
        self._schedule_sync()

    @callback
    def _schedule_sync(self) -> None:
        if self._stopping:
            return
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = self.hass.async_create_task(
            self._async_debounced_sync(),
            "Hardill Alexa exposure sync debounce",
        )

    async def _async_debounced_sync(self) -> None:
        try:
            await asyncio.sleep(SYNC_DEBOUNCE_SECONDS)
            await self.async_sync()
        except asyncio.CancelledError:
            raise

    async def async_sync(self) -> None:
        """Synchronize exposed HA entities and Hardill devices."""
        if self._stopping:
            return
        if self._sync_task is not None and not self._sync_task.done():
            await self._sync_task
            return
        self._sync_task = self.hass.async_create_task(
            self._async_sync_impl(), "Hardill Alexa exposure sync"
        )
        try:
            await self._sync_task
        finally:
            self._sync_task = None

    async def _async_sync_impl(self) -> None:
        specs = self._collect_exposed_specs()
        try:
            remote_devices = await async_fetch_devices(
                self.hass, self.username, self.password
            )
        except HardillInvalidAuth:
            _LOGGER.error("Hardill credentials are no longer valid; sync skipped")
            return
        except HardillCannotConnect as err:
            _LOGGER.warning("Cannot fetch Hardill devices; sync skipped: %s", err)
            return

        remote_by_id = {device.appliance_id: device for device in remote_devices}
        available_by_name: dict[str, list[HardillDevice]] = {}
        for device in remote_devices:
            available_by_name.setdefault(device.friendly_name.casefold(), []).append(device)

        mappings: dict[str, str] = {}
        used_remote: set[str] = set()
        needs_admin = False

        # Preserve old v0.1 mappings if the corresponding entity is still exposed.
        for appliance_id, entity_id in self.legacy_mappings.items():
            if entity_id in specs and appliance_id in remote_by_id:
                mappings[appliance_id] = entity_id
                used_remote.add(appliance_id)

        # Existing integration-managed devices are the most reliable association.
        recreate: set[str] = set()
        update: set[str] = set()
        for entity_id, spec in specs.items():
            record = self._managed.get(entity_id)
            if not record:
                continue
            appliance_id = str(record.get("appliance_id", ""))
            remote = remote_by_id.get(appliance_id)
            if remote is None:
                recreate.add(entity_id)
                needs_admin = True
                continue
            # Hardill does not allow renaming an existing device. Recreate it if
            # HA's friendly name changed.
            if remote.friendly_name != spec.name:
                recreate.add(entity_id)
                needs_admin = True
                continue
            mappings[appliance_id] = entity_id
            used_remote.add(appliance_id)
            if (
                tuple(remote.raw.get("actions") or ()) != spec.actions
                or tuple(remote.raw.get("applianceTypes") or ()) != spec.appliance_types
                or remote.raw.get("friendlyDescription") != spec.description
            ):
                update.add(entity_id)
                needs_admin = True

        # Reuse a manually-created device with exactly the same name if there is
        # exactly one unused match. This avoids duplicates during Node-RED migration.
        for entity_id, spec in specs.items():
            if entity_id in mappings.values() or entity_id in recreate:
                continue
            matches = [
                dev
                for dev in available_by_name.get(spec.name.casefold(), [])
                if dev.appliance_id not in used_remote
            ]
            if len(matches) == 1:
                device = matches[0]
                mappings[device.appliance_id] = entity_id
                used_remote.add(device.appliance_id)
                _LOGGER.info(
                    "Reusing existing Hardill device %s for %s",
                    device.friendly_name,
                    entity_id,
                )
            else:
                needs_admin = True

        removed_managed = set(self._managed) - set(specs)
        if removed_managed:
            needs_admin = True

        if needs_admin:
            try:
                async with HardillWebSession(self.username, self.password) as admin:
                    # Delete integration-managed devices that are no longer exposed.
                    for entity_id in sorted(removed_managed):
                        await self._async_delete_managed(admin, entity_id)

                    # A rename requires delete + recreate because Hardill's edit API
                    # deliberately keeps friendlyName immutable.
                    for entity_id in sorted(recreate):
                        await self._async_delete_managed(admin, entity_id)

                    # Update capabilities of still-existing managed devices.
                    for entity_id in sorted(update - recreate):
                        spec = specs[entity_id]
                        record = self._managed.get(entity_id)
                        if not record or not record.get("mongo_id"):
                            continue
                        updated = await admin.async_update_device(
                            mongo_id=str(record["mongo_id"]),
                            friendly_name=spec.name,
                            friendly_description=spec.description,
                            actions=list(spec.actions),
                            appliance_types=list(spec.appliance_types),
                        )
                        self._managed[entity_id] = _managed_record(updated, spec)

                    # Create any exposed entity that still has no usable remote device.
                    mapped_entities = set(mappings.values())
                    for entity_id, spec in specs.items():
                        if entity_id in mapped_entities and entity_id not in recreate:
                            continue
                        created = await admin.async_create_device(
                            friendly_name=spec.name,
                            friendly_description=spec.description,
                            actions=list(spec.actions),
                            appliance_types=list(spec.appliance_types),
                        )
                        record = _managed_record(created, spec)
                        self._managed[entity_id] = record
                        appliance_id = record["appliance_id"]
                        mappings[appliance_id] = entity_id
                        mapped_entities.add(entity_id)
                        _LOGGER.info(
                            "Created Hardill Alexa device %s for %s",
                            spec.name,
                            entity_id,
                        )
            except HardillInvalidAuth:
                _LOGGER.error(
                    "Hardill web login failed; existing devices remain usable but "
                    "automatic device creation/deletion is unavailable"
                )
            except HardillCannotConnect as err:
                _LOGGER.warning(
                    "Hardill device-management sync failed; existing mappings remain: %s",
                    err,
                )

        await self._store.async_save({"managed": self._managed})
        self.bridge.set_mappings(mappings)
        _LOGGER.info(
            "Hardill Alexa sync complete: %d exposed HA entities, %d active mappings",
            len(specs),
            len(mappings),
        )

    async def _async_delete_managed(
        self, admin: HardillWebSession, entity_id: str
    ) -> None:
        record = self._managed.get(entity_id)
        if not record:
            return
        mongo_id = record.get("mongo_id")
        if mongo_id:
            await admin.async_delete_device(str(mongo_id))
        self._managed.pop(entity_id, None)

    def _collect_exposed_specs(self) -> dict[str, DeviceSpec]:
        states: list[State] = []
        for state in self.hass.states.async_all():
            if not async_should_expose(
                self.hass, EXPOSURE_ASSISTANT, state.entity_id
            ):
                continue
            if _entity_spec(state) is not None:
                states.append(state)

        # Alexa names must be useful and preferably unique. If two exposed entities
        # have the same friendly name, suffix both with their HA object id.
        counts: dict[str, int] = {}
        for state in states:
            base = _base_name(state)
            counts[base.casefold()] = counts.get(base.casefold(), 0) + 1

        result: dict[str, DeviceSpec] = {}
        for state in states:
            spec = _entity_spec(state)
            if spec is None:
                continue
            if counts[_base_name(state).casefold()] > 1:
                object_id = state.entity_id.split(".", 1)[1].replace("_", " ")
                spec = DeviceSpec(
                    entity_id=spec.entity_id,
                    name=f"{spec.name} ({object_id})",
                    description=spec.description,
                    actions=spec.actions,
                    appliance_types=spec.appliance_types,
                )
            result[state.entity_id] = spec
        return result


def _managed_record(payload: dict[str, Any], spec: DeviceSpec) -> dict[str, Any]:
    mongo_id = payload.get("_id")
    appliance_id = payload.get("applianceId")
    if mongo_id is None or appliance_id is None:
        raise HardillCannotConnect("Created Hardill device response misses _id/applianceId")
    return {
        "mongo_id": str(mongo_id),
        "appliance_id": str(appliance_id),
        "friendly_name": spec.name,
        "actions": list(spec.actions),
        "appliance_types": list(spec.appliance_types),
    }


def _base_name(state: State) -> str:
    name = state.name.strip() if state.name else ""
    if name:
        return name
    return state.entity_id.split(".", 1)[1].replace("_", " ").strip()


def _entity_spec(state: State) -> DeviceSpec | None:
    """Return the best legacy Alexa-v2 capabilities for one HA state."""
    entity_id = state.entity_id
    domain = entity_id.split(".", 1)[0]
    name = _base_name(state)
    description = f"{MANAGED_DESCRIPTION_PREFIX}: {entity_id}"
    attrs = state.attributes

    actions: list[str] = []
    appliance_types: list[str] = []

    if domain == "light":
        actions.extend(("turnOn", "turnOff"))
        appliance_types.append("LIGHT")
        modes = {str(mode) for mode in attrs.get("supported_color_modes") or ()}
        if modes - {"onoff"}:
            actions.extend(
                ("setPercentage", "incrementPercentage", "decrementPercentage")
            )
        if modes & {"hs", "xy", "rgb", "rgbw", "rgbww"}:
            actions.append("setColor")
        if "color_temp" in modes:
            actions.append("setColorTemperature")

    elif domain in {"switch", "input_boolean"}:
        actions.extend(("turnOn", "turnOff"))
        appliance_types.append("SWITCH")

    elif domain == "fan":
        actions.extend(("turnOn", "turnOff"))
        appliance_types.append("SWITCH")
        if attrs.get("percentage") is not None:
            actions.extend(
                ("setPercentage", "incrementPercentage", "decrementPercentage")
            )

    elif domain == "cover":
        actions.extend(("turnOn", "turnOff"))
        appliance_types.append("SWITCH")
        if attrs.get("current_position") is not None:
            actions.extend(
                ("setPercentage", "incrementPercentage", "decrementPercentage")
            )

    elif domain == "media_player":
        actions.extend(("turnOn", "turnOff"))
        appliance_types.append("SWITCH")
        if attrs.get("volume_level") is not None:
            actions.extend(
                ("setPercentage", "incrementPercentage", "decrementPercentage")
            )

    elif domain == "vacuum":
        actions.extend(("turnOn", "turnOff"))
        appliance_types.append("SWITCH")

    elif domain == "climate":
        appliance_types.append("THERMOSTAT")
        actions.append("getTemperatureReading")
        if attrs.get("temperature") is not None:
            actions.extend(
                (
                    "setTargetTemperature",
                    "incrementTargetTemperature",
                    "decrementTargetTemperature",
                    "getTargetTemperature",
                )
            )

    elif domain == "sensor" and str(attrs.get("device_class")) == "temperature":
        appliance_types.append("THERMOSTAT")
        actions.append("getTemperatureReading")

    elif domain == "lock":
        appliance_types.append("SMARTLOCK")
        actions.extend(("setLockState", "getLockState"))

    elif domain in {"scene", "script", "button", "input_button", "automation"}:
        appliance_types.append("ACTIVITY_TRIGGER")
        actions.append("turnOn")

    elif domain in {"number", "input_number"}:
        # Alexa percentage is semantically 0..100. Only expose numeric helpers that
        # really use that range, otherwise Alexa would silently change the scale.
        minimum = attrs.get("min")
        maximum = attrs.get("max")
        if minimum is None or maximum is None:
            return None
        try:
            if float(minimum) != 0 or float(maximum) != 100:
                return None
        except (TypeError, ValueError):
            return None
        appliance_types.append("SWITCH")
        actions.extend(
            ("setPercentage", "incrementPercentage", "decrementPercentage")
        )

    else:
        return None

    # Preserve insertion order while eliminating accidental duplicates.
    actions = list(dict.fromkeys(actions))
    appliance_types = list(dict.fromkeys(appliance_types))
    if not actions or not appliance_types:
        return None

    return DeviceSpec(
        entity_id=entity_id,
        name=name,
        description=description,
        actions=tuple(actions),
        appliance_types=tuple(appliance_types),
    )
