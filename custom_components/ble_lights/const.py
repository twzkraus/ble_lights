"""Constants"""
import voluptuous as vol
from enum import Enum

from homeassistant.helpers.config_validation import make_entity_service_schema
import homeassistant.helpers.config_validation as cv

DOMAIN = "ble_lights"
DEVICE_STARTUP_TIMEOUT_SECONDS = 30
DEFAULT_NOTIFY_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
DEFAULT_WRITE_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
# Notifications can arrive as several BLE fragments (MTU-limited) rather than
# one 40-byte chunk - e.g. 5 + 15 + 20 bytes. We buffer fragments until we
# have a full packet. The full packet typically lands within ~2s of the
# request; this timeout is a generous safety margin before we give up and
# discard a partial/stalled sequence rather than let it linger forever.
NOTIFICATION_REASSEMBLY_TIMEOUT_SECONDS = 6

class Schema(Enum):
    """General used service schema definition"""

    SYNC_STATE = make_entity_service_schema(
        {
            vol.Optional("timeout", default=6.0): vol.Coerce(float),
        }
    )