"""Constants for the Hardill Alexa Bridge integration."""

DOMAIN = "hardill_alexa_bridge"

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_MAPPINGS = "mappings"

# Home Assistant only exposes the built-in assistants in the Voice Assistants UI.
# "conversation" is Home Assistant Assist, so we mirror that exposure selection.
EXPOSURE_ASSISTANT = "conversation"

HARDILL_BASE_URL = "https://alexa-node-red.bm.hardill.me.uk"
DEVICES_URL = f"{HARDILL_BASE_URL}/api/v1/devices"
LOGIN_URL = f"{HARDILL_BASE_URL}/login"
MANAGE_DEVICES_URL = f"{HARDILL_BASE_URL}/devices"

MQTT_HOST = "alexa-node-red.hardill.me.uk"
MQTT_PORT = 1883
MQTT_RECONNECT_DELAY = 5
COMMAND_TIMEOUT = 4.0
SYNC_DEBOUNCE_SECONDS = 2.0

STORE_VERSION = 1
STORE_KEY_PREFIX = f"{DOMAIN}.managed_devices"
MANAGED_DESCRIPTION_PREFIX = "Home Assistant entity"
