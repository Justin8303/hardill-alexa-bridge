# Hardill Alexa Bridge for Home Assistant

A Home Assistant custom integration for users of the legacy
`hardillb/node-red-contrib-alexa-home-skill` service.

It removes Node-RED from the control path: Home Assistant connects directly to
Hardill's MQTT bridge and automatically mirrors Home Assistant's **Assist**
exposure selection to Hardill/Alexa.

> [!WARNING]
> This targets the legacy Ben Hardill Alexa Home Skill service, not
> `node-red-contrib-alexa-smart-home` / CB-Net v3. Amazon announced the legacy
> skill would stop working in November 2025. This project is useful only while
> the legacy skill still works for your Alexa account.

## v0.3 automatic exposure sync and Alexa naming

Home Assistant does not let custom integrations register an additional assistant
in the built-in **Settings -> Voice assistants -> Expose** UI. Therefore this
integration uses Home Assistant **Assist** (`conversation`) as its exposure
source:

1. Open **Settings -> Voice assistants -> Expose**.
2. Expose the entities you want to make available to Alexa to **Assist**.
3. Hardill Alexa Bridge watches that setting and synchronizes supported entities.

When an exposed supported entity has no matching Hardill device, the integration
logs into Hardill's web device-management endpoint and creates one automatically.

- Existing Hardill devices with the exact same friendly name are reused.
- Devices created by this integration are remembered in Home Assistant storage.
- If an integration-created entity is no longer exposed, its Hardill device is
  deleted automatically.
- Manually-created Hardill devices are never deleted by the integration.
- Alexa names use the first explicit Home Assistant entity/voice alias when one
  is configured; otherwise the normal Home Assistant friendly name is used.
- Duplicate names are disambiguated with the Home Assistant area first, then the
  device name, and only as a last resort with a stable number. Technical
  `entity_id` fragments are no longer added to Alexa names.
- If an alias, HA name, device name or area changes, an integration-managed
  Hardill device is recreated with the new Alexa name because the legacy Hardill
  UI/API does not support changing a device name.

After devices are added/removed/renamed, ask Alexa:

> Alexa, discover devices

The legacy Hardill service does not provide a client API for forcing Alexa
Discovery from Home Assistant.

## Supported automatic mappings

- `light`: on/off, brightness, color, color temperature depending on supported
  color modes.
- `switch`, `input_boolean`: on/off.
- `fan`: on/off and percentage when available.
- `cover`: open/close and position when available.
- `media_player`: on/off and volume percentage when available.
- `vacuum`: start/stop.
- `climate`: current temperature and target temperature.
- temperature `sensor`: current temperature query.
- `lock`: lock/unlock and state query.
- `scene`, `script`, `button`, `input_button`, `automation`: activity trigger.
- `number` / `input_number`: percentage only when the helper range is exactly
  0..100.

Unsupported entities are simply ignored even if exposed to Assist.

## Migration from Node-RED

1. Install this custom integration.
2. Add **Hardill Alexa Bridge** under **Settings -> Devices & services** and use
   the same Hardill username/password as your Node-RED Alexa configuration.
3. Set the desired entity exposure under **Settings -> Voice assistants ->
   Expose -> Assist**.
4. Disable the old `node-red-contrib-alexa-home-skill` config/node before doing
   the voice-control test.
5. Say **"Alexa, discover devices"** once after the first synchronization.
6. Test one or two devices before removing the Node-RED flow permanently.

The integration tries to reuse existing Hardill devices by exact friendly-name
match, so a normal migration should not create duplicates if names already match.
Legacy v0.1 manual mappings are also honored when upgrading.

## Installation without HACS

Copy:

`custom_components/hardill_alexa_bridge`

into:

`/config/custom_components/hardill_alexa_bridge`

Restart Home Assistant and add **Hardill Alexa Bridge**.

## HACS

The repository layout is HACS-ready. Put this project in a GitHub repository and
add it to HACS as a custom **Integration** repository.

## Protocol / security

The integration uses:

- HTTPS Basic Auth for `/api/v1/devices`.
- Hardill's website session (`/login`, `/devices`, `/device/:id`) to create,
  update and remove integration-managed devices.
- MQTT `alexa-node-red.hardill.me.uk:1883`, matching the original Node-RED
  package, subscribing to `command/<username>/#` and responding on
  `response/<username>/<applianceId>`.

The MQTT leg uses port 1883 without TLS because that is how the legacy Hardill
client protocol is implemented. Do not run the Node-RED Hardill config node and
this integration simultaneously with the same account; both use the username as
MQTT client identity and can disconnect each other.

## Version

- 0.3.1: fix area-name lookup on Home Assistant versions without `device_registry.async_get_effective_area_id()`; child devices still inherit their parent area.
- 0.3.0: prefer HA voice/entity aliases for Alexa names; disambiguate duplicate
  names as area + name, then device + name, with stable numeric fallback; react
  to entity/device/area renames.
- 0.2.0: automatic synchronization from HA Assist exposure; automatic Hardill
  device creation/update/deletion; migration reuse of existing devices.
- 0.1.0: initial manual mapping implementation.
