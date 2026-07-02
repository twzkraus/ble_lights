"""Constants"""
import voluptuous as vol
from enum import Enum

from homeassistant.helpers.config_validation import make_entity_service_schema
import homeassistant.helpers.config_validation as cv

DOMAIN = "generic_bt_twzkraus"
DEVICE_STARTUP_TIMEOUT_SECONDS = 30
DEFAULT_NOTIFY_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
DEFAULT_WRITE_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"

class Schema(Enum):
    """General used service schema definition"""

    WRITE_GATT = make_entity_service_schema(
        {
            vol.Required("target_uuid"): cv.string,
            vol.Required("data"): cv.string
        }
    )
    READ_GATT = make_entity_service_schema(
        {
            vol.Required("target_uuid"): cv.string
        }
    )
    SUBSCRIBE_NOTIFY = make_entity_service_schema(
        {
            vol.Required("target_uuid"): cv.string
        }
    )
    UNSUBSCRIBE_NOTIFY = make_entity_service_schema(
        {
            vol.Required("target_uuid"): cv.string
        }
    )
    # target_uuid defaults to DEFAULT_WRITE_UUID and timeout defaults to the
    # device's own reassembly timeout if not provided - see
    # GenericBTStateSensor.async_request_settings.
    REQUEST_SETTINGS = make_entity_service_schema(
        {
            vol.Optional("target_uuid"): cv.string,
            vol.Optional("timeout", default=6.0): vol.Coerce(float),
        }
    )