"""HTTP API helpers for Hardill's Alexa bridge."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from aiohttp import BasicAuth, ClientError, ClientSession, CookieJar

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEVICES_URL, HARDILL_BASE_URL, LOGIN_URL, MANAGE_DEVICES_URL


class HardillError(Exception):
    """Base exception for Hardill API errors."""


class HardillInvalidAuth(HardillError):
    """Authentication failed."""


class HardillCannotConnect(HardillError):
    """Connection to Hardill service failed."""


@dataclass(frozen=True, slots=True)
class HardillDevice:
    """A device configured in the Hardill Alexa bridge."""

    appliance_id: str
    friendly_name: str
    raw: dict[str, Any]


async def async_fetch_devices(
    hass: HomeAssistant,
    username: str,
    password: str,
) -> list[HardillDevice]:
    """Fetch the user's configured devices from Hardill's bridge."""
    session = async_get_clientsession(hass)

    try:
        async with asyncio.timeout(10):
            async with session.get(
                DEVICES_URL,
                auth=BasicAuth(username, password),
            ) as response:
                if response.status in (401, 403):
                    raise HardillInvalidAuth
                if response.status != 200:
                    raise HardillCannotConnect(
                        f"Unexpected HTTP status {response.status}"
                    )
                payload = await response.json(content_type=None)
    except HardillInvalidAuth:
        raise
    except (TimeoutError, ClientError, ValueError) as err:
        raise HardillCannotConnect from err

    if isinstance(payload, dict):
        rows = list(payload.values())
    elif isinstance(payload, list):
        rows = payload
    else:
        raise HardillCannotConnect("Unexpected device payload")

    devices: list[HardillDevice] = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        appliance_id = row.get("applianceId")
        friendly_name = row.get("friendlyName")
        if appliance_id is None or friendly_name is None:
            continue

        devices.append(
            HardillDevice(
                appliance_id=str(appliance_id),
                friendly_name=str(friendly_name),
                raw=row,
            )
        )

    return devices


class HardillWebSession:
    """Authenticated session for Hardill's web device-management endpoints."""

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "HardillWebSession":
        # A separate cookie jar is required because /devices uses the website's
        # Passport session, unlike /api/v1/devices which accepts HTTP Basic auth.
        self._session = ClientSession(cookie_jar=CookieJar())
        try:
            await self._async_login()
        except Exception:
            await self._session.close()
            self._session = None
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _async_login(self) -> None:
        session = self._require_session()
        try:
            async with asyncio.timeout(10):
                async with session.post(
                    LOGIN_URL,
                    data={"username": self.username, "password": self.password},
                    allow_redirects=False,
                ) as response:
                    if response.status not in (301, 302, 303):
                        raise HardillCannotConnect(
                            f"Unexpected login HTTP status {response.status}"
                        )
                    location = response.headers.get("Location", "")
                    if location.endswith("/login") or location == "/login":
                        raise HardillInvalidAuth
                    if not (location.endswith("/devices") or location == "/devices"):
                        raise HardillCannotConnect(
                            f"Unexpected login redirect {location!r}"
                        )
        except HardillInvalidAuth:
            raise
        except (TimeoutError, ClientError) as err:
            raise HardillCannotConnect from err

    async def async_create_device(
        self,
        *,
        friendly_name: str,
        friendly_description: str,
        actions: list[str],
        appliance_types: list[str],
        entity_id: str | None = None,
    ) -> dict[str, Any]:
        """Create one device and return Hardill's full stored object."""
        payload: dict[str, Any] = {
            "friendlyName": friendly_name,
            "friendlyDescription": friendly_description,
            "actions": actions,
            "applianceTypes": appliance_types,
        }
        # Alexa echoes additionalApplianceDetails in command payloads. Keeping
        # the HA entity id there lets the bridge resolve commands even if Alexa
        # temporarily addresses a stale applianceId after a rename/recreate.
        if entity_id:
            payload["additionalApplianceDetails"] = {
                "extraDetail1": entity_id,
                "extraDetail2": "home_assistant",
            }
        return await self._async_json_request(
            "PUT", MANAGE_DEVICES_URL, payload, expected=(201,)
        )

    async def async_update_device(
        self,
        *,
        mongo_id: str,
        friendly_name: str,
        friendly_description: str,
        actions: list[str],
        appliance_types: list[str],
    ) -> dict[str, Any]:
        """Update capabilities/description of an integration-managed device."""
        # Hardill's endpoint checks username and _id in the JSON body. The UI
        # intentionally does not allow friendlyName to be changed after creation.
        payload = {
            "_id": mongo_id,
            "username": self.username,
            "friendlyName": friendly_name,
            "friendlyDescription": friendly_description,
            "actions": actions,
            "applianceTypes": appliance_types,
        }
        return await self._async_json_request(
            "POST",
            f"{HARDILL_BASE_URL}/device/{mongo_id}",
            payload,
            expected=(200, 201),
        )

    async def async_delete_device(self, mongo_id: str) -> None:
        """Delete one integration-managed Hardill device."""
        session = self._require_session()
        try:
            async with asyncio.timeout(10):
                async with session.delete(
                    f"{HARDILL_BASE_URL}/device/{mongo_id}",
                    allow_redirects=False,
                ) as response:
                    if response.status in (301, 302, 303):
                        location = response.headers.get("Location", "")
                        if location.endswith("/login"):
                            raise HardillInvalidAuth
                    if response.status not in (200, 202, 204):
                        raise HardillCannotConnect(
                            f"Unexpected delete HTTP status {response.status}"
                        )
        except (HardillInvalidAuth, HardillCannotConnect):
            raise
        except (TimeoutError, ClientError) as err:
            raise HardillCannotConnect from err

    async def _async_json_request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any],
        *,
        expected: tuple[int, ...],
    ) -> dict[str, Any]:
        session = self._require_session()
        try:
            async with asyncio.timeout(10):
                async with session.request(
                    method,
                    url,
                    json=payload,
                    allow_redirects=False,
                ) as response:
                    if response.status in (301, 302, 303):
                        location = response.headers.get("Location", "")
                        if location.endswith("/login"):
                            raise HardillInvalidAuth
                    if response.status not in expected:
                        body = await response.text()
                        raise HardillCannotConnect(
                            f"Unexpected HTTP status {response.status}: {body[:200]}"
                        )
                    data = await response.json(content_type=None)
        except (HardillInvalidAuth, HardillCannotConnect):
            raise
        except (TimeoutError, ClientError, ValueError) as err:
            raise HardillCannotConnect from err

        if not isinstance(data, dict):
            raise HardillCannotConnect("Unexpected device-management response")
        return data

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("HardillWebSession is not open")
        return self._session
