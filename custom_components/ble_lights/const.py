"""Constants"""
import voluptuous as vol
from enum import Enum, IntEnum

from homeassistant.helpers.config_validation import make_entity_service_schema
import homeassistant.helpers.config_validation as cv

DOMAIN = "ble_lights"
NUM_COLOR_SLOTS = 6
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

class Direction(IntEnum):
    Left = 0
    Center = 1
    Right = 2

DIRECTION_MAPPING: dict[int, str] = {d.value: d.name for d in Direction}
DIRECTION_NAMES: list[str] = [d.name for d in Direction]
DIRECTIONS: list[tuple[str, int]] = [(d.name, d.value) for d in Direction]
DIRECTION_CODES: dict[str, int] = {d.name: d.value for d in Direction}

class Effect(str, Enum):
    Still = "0"
    Blink = "B"
    Twinkle = "W"
    Chase = "C"
    MovingWave = "M"
    Ants = "A"
    Sparkle = "S"
    WhiteSparkle = "P"
    ThreeBlock = "3"
    Trains = "T"
    CrossFade = "F"
    Blocks = "L"
    BlockGradient = "K"
    Spiral = "I"
    Shimmer = "H"
    GlowWorm = "G"
    Clouds = "Y"
    ColorPulse = "U"
    RandomPlacement = "R"
    ElectricShock = "E"

# Human-readable names differ from the enum member names above (which must be
# valid identifiers), so keep an explicit label map keyed by the enum member.
EFFECT_LABELS: dict[Effect, str] = {
    Effect.Still: "Still",
    Effect.Blink: "Blink",
    Effect.Twinkle: "Twinkle",
    Effect.Chase: "Chase",
    Effect.MovingWave: "Moving Wave",
    Effect.Ants: "Ants",
    Effect.Sparkle: "Sparkle",
    Effect.WhiteSparkle: "White Sparkle",
    Effect.ThreeBlock: "Three Block",
    Effect.Trains: "Trains",
    Effect.CrossFade: "Cross Fade",
    Effect.Blocks: "Blocks",
    Effect.BlockGradient: "Block Gradient",
    Effect.Spiral: "Spiral",
    Effect.Shimmer: "Shimmer",
    Effect.GlowWorm: "Glow Worm",
    Effect.Clouds: "Clouds",
    Effect.ColorPulse: "Color Pulse",
    Effect.RandomPlacement: "Random Placement",
    Effect.ElectricShock: "Electric Shock",
}

# Backward-compatible exports, derived from the enum + label map
PROGRAM_MAPPING: dict[bytes, str] = {
    e.value.encode(): label for e, label in EFFECT_LABELS.items()
}
EFFECTS: list[tuple[str, str]] = [
    (label, e.value) for e, label in EFFECT_LABELS.items()
]
EFFECT_CODES: dict[str, str] = {label: e.value for e, label in EFFECT_LABELS.items()}
CODE_TO_EFFECT: dict[str, str] = {e.value: label for e, label in EFFECT_LABELS.items()}

SYNC_MODE_MAPPING = {
    0: "Standalone",
    1: "Leader",
    2: "Follower"
}

# Default seconds of inactivity before we auto-disconnect.
# 0 / None disables idle-disconnect entirely.
DEFAULT_IDLE_DISCONNECT_SECONDS = 30

# Small buffer added after a device timer's predicted on/off transition
# before we poll, so we're asking "what happened" shortly after the
# transition rather than racing it.
POLL_TIMER_EVENT_BUFFER_SECONDS = 20

# requestSettings response is a fixed 40-byte binary struct, NOT text.
# Layout (little-endian, all unsigned bytes unless noted):
#   1  program
#   1  speed
#   18 colors[6] (hsv triples, 3 bytes each)
#   1  onOffSwitch
#   7  timer1Settings
#   7  timer2Settings
#   1  brightness
#   1  version
#   1  syncMode
#   1  direction
#   1  unused
SETTINGS_PACKET_LENGTH = 40

# "requestSettings" command: [1-byte length of payload below][ASCII payload]
REQUEST_SETTINGS_COMMAND_HEX = "0f7265717565737453657474696e6773"

class Schema(Enum):
    """General used service schema definition"""

    SYNC_STATE = make_entity_service_schema(
        {
            vol.Optional("timeout", default=6.0): vol.Coerce(float),
        }
    )