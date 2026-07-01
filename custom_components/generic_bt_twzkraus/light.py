"""Support for Generic BT light."""
from __future__ import annotations

import colorsys
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.light import (
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_WRITE_UUID, DOMAIN
from .coordinator import GenericBTCoordinator
from .entity import GenericBTEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# --- Protocol -----------------------------------------------------------

CMD_LIGHTS_ON_PREFIX = 0x08
CMD_LIGHTS_OFF_PREFIX = 0x09
CMD_PROGRAM_PREFIX = 0x08
CMD_SET_COLORS_PREFIX = 0x12

ASCII_LIGHTS_ON = "lightsOn"
ASCII_LIGHTS_OFF = "lightsOff"

NUM_COLOR_SLOTS = 6
VALID_COLOR_COUNTS = (1, 2, 3, 6)

# (name, code, supports_multiple_colors)
EFFECTS: list[tuple[str, str]] = [
    ("Still", "0"),
    ("Blink", "B"),
    ("Twinkle", "W"),
    ("Chase", "C"),
    ("Moving Wave", "M"),
    ("Ants", "A"),
    ("Sparkle", "S"),
    ("White Sparkle", "P"),
    ("Three Block", "3"),
    ("Trains", "T"),
    ("Cross Fade", "F"),
    ("Blocks", "L"),
    ("Block Gradient", "K"),
    ("Spiral", "I"),
    ("Shimmer", "H"),
    ("Glow Worm", "G"),
    ("Clouds", "Y"),
    ("Color Pulse", "U"),
    ("Random Placement", "R"),
    ("Electric Shock", "E"),
]
EFFECT_CODES: dict[str, str] = dict(EFFECTS)

SET_COLORS_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Optional(f"color_{i}"): vol.All(
            vol.ExactSequence([cv.byte, cv.byte, cv.byte]), vol.Coerce(tuple)
        )
        for i in range(1, NUM_COLOR_SLOTS + 1)
    }
)


def _ascii_command(prefix: int, ascii_payload: str) -> str:
    """Build a hex payload: 1 prefix byte + ascii-encoded payload."""
    return f"{prefix:02X}" + ascii_payload.encode("ascii").hex().upper()


def _rgb_to_hsl_bytes(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Convert an 0-255 RGB tuple into single-byte H, S, L values.

    ASSUMPTION: H/S/L are each a single byte (0-255). Verify against a
    real device and adjust if colors come out wrong.
    """
    r, g, b = (c / 255 for c in rgb)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return (round(h * 255), round(s * 255), round(l * 255))


def _expand_colors(
    colors: list[tuple[int, int, int]]
) -> list[tuple[int, int, int]]:
    """Expand 1/2/3/6 colors out to the 6 slots the device expects."""
    count = len(colors)
    if count not in VALID_COLOR_COUNTS:
        raise ValueError(f"Expected 1, 2, 3 or 6 colors, got {count}")
    repeats = NUM_COLOR_SLOTS // count
    return (colors * repeats)[:NUM_COLOR_SLOTS]


def _encode_colors(colors: list[tuple[int, int, int]]) -> str:
    """Encode 1/2/3/6 RGB colors into the device's 19-byte hex payload."""
    payload = f"{CMD_SET_COLORS_PREFIX:02X}"
    for rgb in _expand_colors(colors):
        h, s, l = _rgb_to_hsl_bytes(rgb)
        payload += f"{h:02X}{s:02X}{l:02X}"
    return payload


def _encode_effect(code: str) -> str:
    """Encode an effect/program selection into its hex payload."""
    return _ascii_command(CMD_PROGRAM_PREFIX, f"program{code}")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Generic BT light based on a config entry."""
    coordinator: GenericBTCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([GenericBTLight(coordinator)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "set_colors", SET_COLORS_SCHEMA, "async_set_colors"
    )


class GenericBTLight(GenericBTEntity, LightEntity):
    """Representation of a Generic BT Light."""

    _attr_name = None
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = [name for name, _code in EFFECTS]

    def __init__(self, coordinator: GenericBTCoordinator) -> None:
        """Initialize the light."""
        super().__init__(coordinator)
        self._attr_is_on = False
        self._attr_rgb_color = (255, 255, 255)
        self._attr_effect = EFFECTS[0][0]

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on, optionally setting color/effect at the same time."""
        await self._device.write_gatt(
            DEFAULT_WRITE_UUID, _ascii_command(CMD_LIGHTS_ON_PREFIX, ASCII_LIGHTS_ON)
        )
        self._attr_is_on = True

        if ATTR_RGB_COLOR in kwargs:
            await self.async_set_colors(color_1=kwargs[ATTR_RGB_COLOR])

        if ATTR_EFFECT in kwargs:
            await self._async_apply_effect(kwargs[ATTR_EFFECT])

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self._device.write_gatt(
            DEFAULT_WRITE_UUID, _ascii_command(CMD_LIGHTS_OFF_PREFIX, ASCII_LIGHTS_OFF)
        )
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_set_colors(self, **kwargs: Any) -> None:
        """Entity service: set 1, 2, 3 or 6 colors (color_1..color_6)."""
        colors = [
            kwargs[f"color_{i}"]
            for i in range(1, NUM_COLOR_SLOTS + 1)
            if f"color_{i}" in kwargs
        ]
        if len(colors) not in VALID_COLOR_COUNTS:
            raise ValueError(
                f"Provide 1, 2, 3 or 6 colors (color_1..color_6), got {len(colors)}"
            )

        await self._device.write_gatt(DEFAULT_WRITE_UUID, _encode_colors(colors))
        self._attr_rgb_color = colors[0]
        self.async_write_ha_state()

    async def _async_apply_effect(self, effect: str) -> None:
        """Send an effect/program selection to the light."""
        code = EFFECT_CODES.get(effect)
        if code is None:
            raise ValueError(f"Unknown effect: {effect}")
        await self._device.write_gatt(DEFAULT_WRITE_UUID, _encode_effect(code))
        self._attr_effect = effect