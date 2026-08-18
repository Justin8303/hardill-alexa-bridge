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
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
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
        # Names, aliases and area/device assignments can all affect the Alexa
        # friendly name. Exposure changes have their own listener above.
        self._unsubs.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_updated
            )
        )
        self._unsubs.append(
            self.hass.bus.async_listen(
                ar.EVENT_AREA_REGISTRY_UPDATED, self._registry_updated
            )
        )
        self._unsubs.append(
            self.hass.bus.async_listen(
                dr.EVENT_DEVICE_REGISTRY_UPDATED, self._registry_updated
            )
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
        """Collect exposed entities and derive human-friendly Alexa names.

        Naming priority:
        1. First explicit Home Assistant voice/entity alias.
        2. Home Assistant friendly name.
        3. For collisions, prefix the area name.
        4. If that still collides, add the device name.
        5. As a last resort, use a stable numeric suffix instead of an entity_id.
        """
        candidates: dict[str, tuple[DeviceSpec, str | None, str | None]] = {}

        for state in self.hass.states.async_all():
            if not async_should_expose(
                self.hass, EXPOSURE_ASSISTANT, state.entity_id
            ):
                continue

            preferred_name = self._preferred_name(state)
            spec = _entity_spec(state, preferred_name)
            if spec is None:
                continue

            candidates[state.entity_id] = (
                spec,
                self._area_name(state.entity_id),
                self._device_name(state.entity_id),
            )

        names = {entity_id: item[0].name for entity_id, item in candidates.items()}

        # First disambiguation level: area + name, e.g.
        # "Wohnzimmer Deckenlampe" and "Schlafzimmer Deckenlampe".
        for group in _duplicate_name_groups(names):
            for entity_id in group:
                _spec, area_name, _device_name = candidates[entity_id]
                if area_name:
                    names[entity_id] = _combine_name(area_name, names[entity_id])

        # Second level: device name. This handles two same-named entities in one
        # area without leaking a technical entity_id into Alexa.
        for group in _duplicate_name_groups(names):
            for entity_id in group:
                _spec, _area_name, device_name = candidates[entity_id]
                if device_name:
                    names[entity_id] = _combine_name(device_name, names[entity_id])

        # Last resort: deterministic numbering. Sorting by entity_id keeps the
        # assignment stable across Home Assistant restarts.
        for group in _duplicate_name_groups(names):
            for index, entity_id in enumerate(sorted(group), start=1):
                names[entity_id] = f"{names[entity_id]} {index}"

        result: dict[str, DeviceSpec] = {}
        for entity_id, (spec, _area_name, _device_name) in candidates.items():
            final_name = names[entity_id]
            if final_name != spec.name:
                spec = DeviceSpec(
                    entity_id=spec.entity_id,
                    name=final_name,
                    description=spec.description,
                    actions=spec.actions,
                    appliance_types=spec.appliance_types,
                )
            result[entity_id] = spec

        return result

    def _preferred_name(self, state: State) -> str:
        """Return explicit voice alias first, otherwise HA's friendly name."""
        entity_registry = er.async_get(self.hass)
        if (entry := entity_registry.async_get(state.entity_id)) is not None:
            # RegistryEntry.aliases can also contain Home Assistant's internal
            # COMPUTED_NAME sentinel. Only actual strings are user aliases.
            for alias in entry.aliases:
                if isinstance(alias, str) and (alias := alias.strip()):
                    return alias

        return _base_name(state)

    def _area_name(self, entity_id: str) -> str | None:
        """Return the effective Home Assistant area name for an entity."""
        entity_registry = er.async_get(self.hass)
        entry = entity_registry.async_get(entity_id)
        if entry is None:
            return None

        area_id = entry.area_id
        if area_id is None and entry.device_id is not None:
            device_registry = dr.async_get(self.hass)
            if (device := device_registry.async_get(entry.device_id)) is not None:
                area_id = dr.async_get_effective_area_id(self.hass, device)

        if area_id is None:
            return None
        area = ar.async_get(self.hass).async_get_area(area_id)
        if area is None:
            return None
        return _clean_name(area.name)

    def _device_name(self, entity_id: str) -> str | None:
        """Return the user-visible HA device name for an entity, if any."""
        entity_registry = er.async_get(self.hass)
        entry = entity_registry.async_get(entity_id)
        if entry is None or entry.device_id is None:
            return None
        device = dr.async_get(self.hass).async_get(entry.device_id)
        if device is None:
            return None
        return _clean_name(device.name_by_user or device.name)


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


def _clean_name(value: str | None) -> str | None:
    """Normalize whitespace in a user-facing name."""
    if not value:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _combine_name(prefix: str, name: str) -> str:
    """Combine two display-name parts without obvious duplication."""
    prefix = _clean_name(prefix) or ""
    name = _clean_name(name) or ""
    if not prefix:
        return name
    if not name:
        return prefix

    prefix_folded = prefix.casefold()
    name_folded = name.casefold()
    if name_folded == prefix_folded:
        return name
    if name_folded.startswith(f"{prefix_folded} "):
        return name
    if name_folded.endswith(f" {prefix_folded}"):
        return name
    return f"{prefix} {name}"


def _duplicate_name_groups(names: dict[str, str]) -> list[list[str]]:
    """Return groups of entity IDs whose current names collide."""
    groups: dict[str, list[str]] = {}
    for entity_id, name in names.items():
        groups.setdefault(name.casefold(), []).append(entity_id)
    return [group for group in groups.values() if len(group) > 1]


def _base_name(state: State) -> str:
    if name := _clean_name(state.name):
        return name
    object_id = state.entity_id.split(".", 1)[1].replace("_", " ")
    return _clean_name(object_id) or state.entity_id


def _entity_spec(state: State, preferred_name: str | None = None) -> DeviceSpec | None:
    """Return the best legacy Alexa-v2 capabilities for one HA state."""
    entity_id = state.entity_id
    domain = entity_id.split(".", 1)[0]
    name = _clean_name(preferred_name) or _base_name(state)
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
