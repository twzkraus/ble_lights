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

# Color palette keys mapped to array of 1-6 hsv values
COLOR_PALETTES: dict[str, [[int]]] = {
    "4th of July": [[0, 255, 255], [0, 0, 255], [170, 255, 255]],
    "Candy": [[76, 151, 255], [215, 239, 255], [41, 255, 255], [0, 0, 255]],
    "Cherry Blossom": [[238, 175, 255], [0, 0, 255], [225, 202, 255]],
    "Christmas": [[0, 255, 255], [0, 0, 255], [80, 255, 255]],
    "Confetti": [[234, 239, 255], [58, 243, 255], [127, 215, 255], [23, 219, 255]],
    "Diamonds": [[44, 241, 255], [41, 97, 255]],
    "Easter": [[81, 145, 255], [214, 139, 255], [43, 179, 255], [193, 227, 255], [135, 175, 255]],
    "Fall Green": [[62, 235, 185], [41, 211, 231], [30, 234, 227], [56, 235, 178], [44, 247, 171], [24, 255, 167]],
    "Fall Red": [[24, 255, 255], [40, 255, 255], [52, 255, 117], [20, 255, 255], [0, 255, 104], [45, 255, 112]],
    "Five Color": [[90, 255, 255], [0, 255, 255], [39, 255, 255], [170, 255, 255], [24, 255, 255], [199, 255, 255]],
    "Go Pack Go": [[43, 255, 255], [78, 255, 83], [0, 0, 135], [78, 255, 83], [43, 255, 255], [78, 255, 83]],
    "Halloween": [[25, 255, 255], [203, 255, 109], [203, 255, 109]],
    "Hydrangea": [[191, 255, 118], [126, 255, 255], [212, 255, 255], [0, 0, 255]],
    "Lindsay": [[136, 198, 185], [221, 204, 231], [154, 199, 227], [210, 227, 178], [124, 239, 255], [203, 185, 255]],
    "Mardi Gras": [[90, 255, 255], [193, 255, 255], [44, 255, 255]],
    "Moonlight": [[197, 255, 255], [40, 177, 255], [39, 220, 255], [201, 255, 255], [0, 0, 255]],
    "Rainbow": [[0, 255, 255], [24, 255, 255], [39, 255, 255], [90, 255, 255], [170, 255, 255], [199, 255, 255]],
    "Sapphire": [[168, 255, 255], [160, 198, 255], [159, 175, 255], [161, 206, 255], [158, 174, 255], [160, 204, 255]],
    "St. Patrick's Day": [[79, 255, 255], [51, 255, 255], [78, 255, 83], [51, 118, 255], [96, 255, 183]],
    "Starry Night": [[199, 255, 179], [197, 255, 122], [201, 255, 161], [0, 0, 173], [195, 255, 97], [193, 255, 148]],
    "Under the Sea": [[193, 255, 185], [106, 220, 172], [139, 224, 255], [156, 255, 255], [109, 217, 176], [184, 255, 255]],
    "Valentine's Day": [[238, 175, 255], [253, 48, 255], [242, 255, 255], [252, 255, 255], [222, 155, 255], [0, 255, 115]],
}

COLOR_PALETTE_NAMES = list(COLOR_PALETTES.keys())

class Schema(Enum):
    """General used service schema definition"""

    SYNC_STATE = make_entity_service_schema(
        {
            vol.Optional("timeout", default=6.0): vol.Coerce(float),
        }
    )