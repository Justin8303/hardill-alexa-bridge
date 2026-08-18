"""MQTT bridge between Hardill Alexa and Home Assistant entities."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any

from aiomqtt import Client, MqttError

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, State

from .const import (
    COMMAND_TIMEOUT,
    MQTT_HOST,
    MQTT_PORT,
    MQTT_RECONNECT_DELAY,
)

_LOGGER = logging.getLogger(__name__)


class UnsupportedCommand(Exception):
    """Raised when a command cannot be mapped to the selected HA entity."""


class HardillAlexaBridge:
    """Maintain the Hardill MQTT connection and dispatch Alexa commands."""

    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password: str,
        mappings: dict[str, str],
    ) -> None:
        self.hass = hass
        self.username = username
        self.password = password
        self.mappings = {str(key): value for key, value in mappings.items() if value}
        self._task: asyncio.Task[None] | None = None
        self._client: Client | None = None
        self._stopping = False

    def set_mappings(self, mappings: dict[str, str]) -> None:
        """Replace the active Hardill appliance-to-entity mapping."""
        self.mappings = {
            str(key): value for key, value in mappings.items() if value
        }

    async def async_start(self) -> None:
        """Start the MQTT worker."""
        if self._task is not None:
            return
        self._stopping = False
        self._task = self.hass.async_create_task(
            self._async_run(),
            "Hardill Alexa Bridge MQTT",
        )

    async def async_stop(self) -> None:
        """Stop the MQTT worker."""
        self._stopping = True
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self._client = None

    async def _async_run(self) -> None:
        """Connect, subscribe and reconnect forever."""
        topic = f"command/{self.username}/#"

        while not self._stopping:
            try:
                async with Client(
                    MQTT_HOST,
                    MQTT_PORT,
                    username=self.username,
                    password=self.password,
                    identifier=self.username,
                    keepalive=60,
                ) as client:
                    self._client = client
                    await client.subscribe(topic)
                    _LOGGER.info("Connected to Hardill Alexa MQTT bridge")

                    async for message in client.messages:
                        if self._stopping:
                            return
                        self.hass.async_create_task(
                            self._async_handle_message(message.payload),
                            "Hardill Alexa command",
                        )
            except asyncio.CancelledError:
                raise
            except MqttError as err:
                self._client = None
                if self._stopping:
                    return
                _LOGGER.warning(
                    "Hardill MQTT connection lost (%s); retrying in %s seconds",
                    err,
                    MQTT_RECONNECT_DELAY,
                )
                await asyncio.sleep(MQTT_RECONNECT_DELAY)
            except Exception:
                self._client = None
                if self._stopping:
                    return
                _LOGGER.exception(
                    "Unexpected Hardill MQTT error; retrying in %s seconds",
                    MQTT_RECONNECT_DELAY,
                )
                await asyncio.sleep(MQTT_RECONNECT_DELAY)

    async def _async_handle_message(self, raw_payload: Any) -> None:
        """Handle one Hardill command."""
        try:
            if isinstance(raw_payload, bytes):
                raw_payload = raw_payload.decode("utf-8")
            message = json.loads(raw_payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            _LOGGER.warning("Received malformed Hardill Alexa payload")
            return

        header = message.get("header") or {}
        payload = message.get("payload") or {}
        appliance = payload.get("appliance") or {}

        command = header.get("name")
        message_id = header.get("messageId")
        appliance_id = appliance.get("applianceId")

        if not command or message_id is None or appliance_id is None:
            _LOGGER.warning("Received incomplete Hardill Alexa command: %s", message)
            return

        appliance_id = str(appliance_id)
        entity_id = self.mappings.get(appliance_id)

        if not entity_id:
            _LOGGER.warning(
                "Alexa device %s is not mapped to a Home Assistant entity",
                appliance_id,
            )
            await self._async_ack(message_id, appliance_id, False)
            return

        try:
            async with asyncio.timeout(COMMAND_TIMEOUT):
                extra = await self._async_execute(command, payload, entity_id)
        except (TimeoutError, UnsupportedCommand, ValueError) as err:
            _LOGGER.warning(
                "Failed to handle %s for %s: %s",
                command,
                entity_id,
                err,
            )
            await self._async_ack(message_id, appliance_id, False)
            return
        except Exception:
            _LOGGER.exception("Error handling %s for %s", command, entity_id)
            await self._async_ack(message_id, appliance_id, False)
            return

        await self._async_ack(message_id, appliance_id, True, extra)

    async def _async_ack(
        self,
        message_id: str,
        appliance_id: str,
        success: bool,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Acknowledge a command back to Hardill's service."""
        client = self._client
        if client is None:
            _LOGGER.warning("Cannot acknowledge Alexa command: MQTT is disconnected")
            return

        response: dict[str, Any] = {
            "messageId": message_id,
            "success": success,
        }
        if extra is not None:
            response["extra"] = extra

        topic = f"response/{self.username}/{appliance_id}"
        try:
            await client.publish(topic, json.dumps(response))
        except MqttError as err:
            _LOGGER.warning("Failed to publish Alexa acknowledgement: %s", err)

    async def _async_execute(
        self,
        command: str,
        payload: dict[str, Any],
        entity_id: str,
    ) -> dict[str, Any] | None:
        """Translate one Alexa command into a Home Assistant service call."""
        state = self._require_state(entity_id)
        domain = entity_id.split(".", 1)[0]

        if command == "TurnOnRequest":
            await self._async_turn_on(domain, entity_id)
            return None

        if command == "TurnOffRequest":
            await self._async_turn_off(domain, entity_id)
            return None

        if command == "SetPercentageRequest":
            value = float(payload["percentageState"]["value"])
            await self._async_set_percentage(domain, entity_id, value)
            return None

        if command in ("IncrementPercentageRequest", "DecrementPercentageRequest"):
            delta = float(payload["deltaPercentage"]["value"])
            if command == "DecrementPercentageRequest":
                delta *= -1
            value = self._current_percentage(domain, state) + delta
            await self._async_set_percentage(domain, entity_id, value)
            return None

        if command == "SetColorRequest":
            if domain != "light":
                raise UnsupportedCommand("Color control requires a light entity")
            color = payload["color"]
            hue = float(color["hue"])
            saturation = float(color["saturation"])
            brightness = float(color.get("brightness", 1.0))
            if saturation <= 1:
                saturation *= 100
            if brightness <= 1:
                brightness *= 100
            await self._async_call(
                "light",
                "turn_on",
                entity_id,
                hs_color=(hue, _clamp(saturation, 0, 100)),
                brightness_pct=_clamp(brightness, 0, 100),
            )
            return {"achievedState": {"color": color}}

        if command == "SetColorTemperatureRequest":
            if domain != "light":
                raise UnsupportedCommand(
                    "Color temperature control requires a light entity"
                )
            value = float(payload["colorTemperature"]["value"])
            kelvin = value if value >= 1000 else 1_000_000 / value
            await self._async_call(
                "light",
                "turn_on",
                entity_id,
                color_temp_kelvin=round(kelvin),
            )
            return {"achievedState": payload}

        if command == "SetTargetTemperatureRequest":
            if domain != "climate":
                raise UnsupportedCommand(
                    "Target temperature control requires a climate entity"
                )
            value = float(payload["targetTemperature"]["value"])
            await self._async_call(
                "climate",
                "set_temperature",
                entity_id,
                temperature=value,
            )
            return {"targetTemperature": {"value": value}}

        if command in (
            "IncrementTargetTemperatureRequest",
            "DecrementTargetTemperatureRequest",
        ):
            if domain != "climate":
                raise UnsupportedCommand(
                    "Target temperature control requires a climate entity"
                )
            delta = float(payload["deltaTemperature"]["value"])
            if command == "DecrementTargetTemperatureRequest":
                delta *= -1
            current = state.attributes.get("temperature")
            if current is None:
                raise UnsupportedCommand("Climate entity has no target temperature")
            value = float(current) + delta
            await self._async_call(
                "climate",
                "set_temperature",
                entity_id,
                temperature=value,
            )
            return {"targetTemperature": {"value": value}}

        if command == "GetTemperatureReadingRequest":
            value = self._temperature_reading(state)
            return {"temperatureReading": {"value": value}}

        if command == "GetTargetTemperatureRequest":
            value = state.attributes.get("temperature")
            if value is None:
                raise UnsupportedCommand("Entity has no target temperature")
            return {"targetTemperature": {"value": float(value)}}

        if command in ("SetLockStateRequest", "SetLockState"):
            if domain != "lock":
                raise UnsupportedCommand("Lock control requires a lock entity")
            requested = str(payload.get("lockState", "")).upper()
            if requested == "LOCKED":
                await self._async_call("lock", "lock", entity_id)
            elif requested == "UNLOCKED":
                await self._async_call("lock", "unlock", entity_id)
            else:
                raise UnsupportedCommand(f"Unknown lock state {requested!r}")
            return {"lockState": requested}

        if command in ("GetLockStateRequest", "GetLockState"):
            if domain != "lock":
                raise UnsupportedCommand("Lock state requires a lock entity")
            if state.state == "locked":
                lock_state = "LOCKED"
            elif state.state == "unlocked":
                lock_state = "UNLOCKED"
            else:
                raise UnsupportedCommand(f"Unsupported lock state {state.state!r}")
            return {"lockState": lock_state}

        raise UnsupportedCommand(f"Unsupported Hardill command {command}")

    def _require_state(self, entity_id: str) -> State:
        state = self.hass.states.get(entity_id)
        if state is None:
            raise UnsupportedCommand(f"Entity {entity_id} does not exist")
        if state.state in ("unknown", "unavailable"):
            raise UnsupportedCommand(f"Entity {entity_id} is {state.state}")
        return state

    async def _async_turn_on(self, domain: str, entity_id: str) -> None:
        service_by_domain = {
            "cover": "open_cover",
            "vacuum": "start",
            "button": "press",
            "input_button": "press",
            "automation": "trigger",
        }
        service = service_by_domain.get(domain, "turn_on")
        await self._async_call(domain, service, entity_id)

    async def _async_turn_off(self, domain: str, entity_id: str) -> None:
        service_by_domain = {
            "cover": "close_cover",
            "vacuum": "stop",
        }
        service = service_by_domain.get(domain, "turn_off")
        await self._async_call(domain, service, entity_id)

    async def _async_set_percentage(
        self,
        domain: str,
        entity_id: str,
        value: float,
    ) -> None:
        value = _clamp(value, 0, 100)

        if domain == "light":
            await self._async_call(
                "light", "turn_on", entity_id, brightness_pct=value
            )
            return
        if domain == "fan":
            await self._async_call("fan", "set_percentage", entity_id, percentage=value)
            return
        if domain == "cover":
            await self._async_call(
                "cover", "set_cover_position", entity_id, position=value
            )
            return
        if domain == "media_player":
            await self._async_call(
                "media_player", "volume_set", entity_id, volume_level=value / 100
            )
            return
        if domain in ("number", "input_number"):
            await self._async_call(domain, "set_value", entity_id, value=value)
            return

        raise UnsupportedCommand(
            f"Percentage control is not implemented for domain {domain}"
        )

    def _current_percentage(self, domain: str, state: State) -> float:
        if domain == "light":
            brightness = state.attributes.get("brightness")
            return 0 if brightness is None else float(brightness) / 255 * 100
        if domain == "fan":
            return float(state.attributes.get("percentage") or 0)
        if domain == "cover":
            return float(state.attributes.get("current_position") or 0)
        if domain == "media_player":
            return float(state.attributes.get("volume_level") or 0) * 100
        if domain in ("number", "input_number"):
            return float(state.state)
        raise UnsupportedCommand(
            f"Relative percentage control is not implemented for domain {domain}"
        )

    def _temperature_reading(self, state: State) -> float:
        value = state.attributes.get("current_temperature")
        if value is None:
            value = state.state
        try:
            return float(value)
        except (TypeError, ValueError) as err:
            raise UnsupportedCommand("Entity has no numeric temperature") from err

    async def _async_call(
        self,
        domain: str,
        service: str,
        entity_id: str,
        **service_data: Any,
    ) -> None:
        data = {ATTR_ENTITY_ID: entity_id, **service_data}
        await self.hass.services.async_call(
            domain,
            service,
            data,
            blocking=True,
        )


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
