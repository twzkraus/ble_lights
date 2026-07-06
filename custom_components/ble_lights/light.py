"""Support for Generic BT light."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from bleak.exc import BleakError

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import COLOR_PALETTES, COLOR_PALETTE_NAMES, DEFAULT_WRITE_UUID, DOMAIN
from .coordinator import GenericBTCoordinator
from .entity import GenericBTEntity
from .generic_bt_api.device import SETTINGS_PACKET_LENGTH, NUM_COLOR_SLOTS, parse_settings_packet

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

# --- Protocol -----------------------------------------------------------

DIRECTIONS: list[tuple[str, int]] = [
    ("Left", 0),
    ("Center", 1),
    ("Right", 2),
]
DIRECTION_CODES: dict[str, int] = dict(DIRECTIONS)

# (name, code)
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
CODE_TO_EFFECT: dict[str, str] = {code: name for name, code in EFFECTS}

def _validate_settings_hex(value: str) -> str:
    """Validate that value is a hex string decoding to exactly SETTINGS_PACKET_LENGTH bytes."""
    try:
        raw_bytes = bytes.fromhex(value)
    except ValueError as exc:
        raise vol.Invalid(f"settings must be a valid hex string: {exc}") from exc
    if len(raw_bytes) != SETTINGS_PACKET_LENGTH:
        raise vol.Invalid(
            f"settings must decode to exactly {SETTINGS_PACKET_LENGTH} bytes, "
            f"got {len(raw_bytes)}"
        )
    return value

TURN_ON_CUSTOM_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Optional("color_palette"): vol.In(COLOR_PALETTE_NAMES),
        vol.Optional("effect"): vol.In([name for name, _code in EFFECTS]),
        vol.Optional("brightness"): cv.byte,
        vol.Optional("speed"): cv.byte,
        vol.Optional("direction"): vol.In([name for name, _code in DIRECTIONS]),
        vol.Optional("color_1"): vol.All(vol.ExactSequence([cv.byte, cv.byte, cv.byte]), vol.Coerce(tuple)),
        vol.Optional("color_2"): vol.All(vol.ExactSequence([cv.byte, cv.byte, cv.byte]), vol.Coerce(tuple)),
        vol.Optional("color_3"): vol.All(vol.ExactSequence([cv.byte, cv.byte, cv.byte]), vol.Coerce(tuple)),
        vol.Optional("color_4"): vol.All(vol.ExactSequence([cv.byte, cv.byte, cv.byte]), vol.Coerce(tuple)),
        vol.Optional("color_5"): vol.All(vol.ExactSequence([cv.byte, cv.byte, cv.byte]), vol.Coerce(tuple)),
        vol.Optional("color_6"): vol.All(vol.ExactSequence([cv.byte, cv.byte, cv.byte]), vol.Coerce(tuple)),

    }
)

SET_APPLY_SCENE_SCHEMA = cv.make_entity_service_schema(
    {vol.Required("settings"): vol.All(cv.string, _validate_settings_hex)}
)

# Attributes decoded from device notifications that belong on the light,
# as opposed to purely diagnostic fields (timer1/timer2/version/sync_mode)
# which stay on GenericBTStateSensor only.
LIGHT_RELEVANT_ATTRS = ("colors", "speed", "direction", "raw_hex")

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Generic BT light based on a config entry."""
    coordinator: GenericBTCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([GenericBTLight(coordinator)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "turn_on", TURN_ON_CUSTOM_SCHEMA, "async_turn_on_custom"
    )
    platform.async_register_entity_service(
        "apply_scene", SET_APPLY_SCENE_SCHEMA, "async_apply_scene"
    )


class GenericBTLight(GenericBTEntity, LightEntity, RestoreEntity):
    """Representation of a Generic BT Light.

    Deliberately a simple toggle + brightness + effect entity. This device
    supports up to 6 simultaneous colors, which doesn't map onto any core
    HA color mode (they all assume a single active color) - multi-color
    palettes are handled via the set_colors service and surfaced as an
    attribute, not as entity color state. See set_colors / colors attribute.
    """

    _attr_name = None
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = [name for name, _code in EFFECTS]

    def __init__(self, coordinator: GenericBTCoordinator) -> None:
        """Initialize the light."""
        super().__init__(coordinator)
        self._attr_is_on = False
        self._attr_effect = EFFECTS[0][0]
        self._attr_brightness = 255
        self._attr_extra_state_attributes: dict[str, Any] = {}
        self._device.set_state_callback(self._handle_state_update)

    async def async_added_to_hass(self) -> None:
        """Restore last known state, then reconcile against live device data."""
        await super().async_added_to_hass()
        self._refresh_from_device()
        if not self._attr_is_on and self._device.last_notification_data is None:
            restored_state = await self.async_get_last_state()
            if restored_state is not None and restored_state.state not in (
                None,
                "unknown",
                "unavailable",
            ):
                self._attr_is_on = restored_state.state == "on"
                if restored_state.attributes.get(ATTR_BRIGHTNESS) is not None:
                    self._attr_brightness = restored_state.attributes[ATTR_BRIGHTNESS]
                if restored_state.attributes.get(ATTR_EFFECT) is not None:
                    self._attr_effect = restored_state.attributes[ATTR_EFFECT]
        self.async_write_ha_state()

    @callback
    def _handle_state_update(self) -> None:
        """Refresh entity state when the device pushes a new complete notification.

        This is the same callback GenericBTStateSensor uses, so the light
        stays truthful about is_on/brightness/effect regardless of whether
        the change came from this entity, a physical remote, or another app.
        """
        self._refresh_from_device()
        self.async_write_ha_state()

    def _refresh_from_device(self) -> None:
        data = self._device.last_notification_data
        if data is None:
            return

        self._attr_is_on = bool(data.get("is_on"))

        if (brightness := data.get("brightness")) is not None:
            self._attr_brightness = brightness

        if (program := data.get("program")) is not None:
            effect_name = CODE_TO_EFFECT.get(program)
            if effect_name is not None:
                self._attr_effect = effect_name
            else:
                _LOGGER.debug("Unrecognized program code from device: %s", program)

        self._attr_extra_state_attributes = {
            key: value for key, value in data.items() if key in LIGHT_RELEVANT_ATTRS
        }

    # ---------------------------- Built-in Entity Services --------------------------------
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on, optionally setting effect/brightness at the same time."""
        await self._device.turn_on(DEFAULT_WRITE_UUID)
        self._attr_is_on = True

        if ATTR_EFFECT in kwargs:
            await self._async_apply_effect(kwargs[ATTR_EFFECT])

        if ATTR_BRIGHTNESS in kwargs:
            await self._async_apply_brightness(kwargs[ATTR_BRIGHTNESS])

        self.async_write_ha_state()
        await self._async_confirm_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self._device.turn_off(DEFAULT_WRITE_UUID)
        self._attr_is_on = False
        self.async_write_ha_state()
        await self._async_confirm_state()


    # ---------------------------- Custom Services --------------------------------
    async def async_turn_on_custom(self, **kwargs: Any) -> None:
        """Turn the light on, optionally setting colors, effect, brightness, speed, and direction at the same time."""
        was_on = self._attr_is_on

        # --- Validate everything up front, before touching the device at all ---
        direction_code = None
        if "direction" in kwargs:
            direction_code = DIRECTION_CODES.get(kwargs["direction"])
            if direction_code is None:
                raise ValueError(f"Unknown direction: {kwargs['direction']}")

        palette_name = None
        colors = None
        if "color_palette" in kwargs:
            palette_name = kwargs["color_palette"]
            if palette_name not in COLOR_PALETTE_NAMES:
                raise ValueError(f"Unknown color_palette: {palette_name}")
            colors = COLOR_PALETTES[palette_name]
        else:
            plain_colors = [
                kwargs[f"color_{i}"]
                for i in range(1, NUM_COLOR_SLOTS + 1)
                if f"color_{i}" in kwargs
            ]
            if plain_colors:
                colors = plain_colors

        effect = kwargs.get(ATTR_EFFECT)
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        speed = kwargs.get("speed")

        num_changes = sum(
            x is not None for x in (direction_code, colors, effect, brightness, speed)
        )

        # If it's already on and we're changing a lot at once, cut the lights,
        # apply everything in the dark, and bring it back up clean.
        should_cycle_off = was_on and num_changes >= 3
        if should_cycle_off:
            await self._device.turn_off(DEFAULT_WRITE_UUID)
            self._attr_is_on = False
            self.async_write_ha_state()

        # -- Apply settings --
        if direction_code is not None:
            await self._device.set_direction(DEFAULT_WRITE_UUID, direction_code)

        if colors is not None:
            if palette_name is not None:
                await self._device.set_colors_hsv(DEFAULT_WRITE_UUID, colors)
                if self.coordinator.palette_select_entity is not None:
                    self.coordinator.palette_select_entity.set_palette_option(palette_name)
            else:
                await self._device.set_colors(DEFAULT_WRITE_UUID, colors)
                if self.coordinator.palette_select_entity is not None:
                    self.coordinator.palette_select_entity.invalidate_palette()

        if effect is not None:
            await self._async_apply_effect(effect)

        if brightness is not None:
            await self._async_apply_brightness(brightness)

        if speed is not None:
            await self._device.set_speed(DEFAULT_WRITE_UUID, speed)

        # Only send turn_on if the light was actually off or we turned it off
        if not was_on or should_cycle_off:
            await self._device.turn_on(DEFAULT_WRITE_UUID)
            self._attr_is_on = True

        self.async_write_ha_state()
        await self._async_confirm_state()

    async def async_apply_scene(self, settings: str) -> None:
        """Entity service: take a 40-byte requestSettings hex string (as
        captured from a currentSettings notification) and push the VISIBLE
        ASPECTS back to the device: all 6 colors, program, direction, speed,
        brightness, and on/off state.
        timer1/timer2, sync_mode, and version are ignored.
        """
        raw_bytes = bytes.fromhex(settings)

        parsed = parse_settings_packet(raw_bytes)

        if parsed["is_on"]:
            await self._device.turn_on(DEFAULT_WRITE_UUID)
        else:
            await self._device.turn_off(DEFAULT_WRITE_UUID)

        hsv_colors = [
            (c["hue"], c["saturation"], c["value"]) for c in parsed["colors"]
        ]
        await self._device.set_colors_hsv(DEFAULT_WRITE_UUID, hsv_colors)
        if self.coordinator.palette_select_entity is not None:
            self.coordinator.palette_select_entity.invalidate_palette()

        await self._device.set_effect(DEFAULT_WRITE_UUID, parsed["program_code"])
        await self._device.set_direction(DEFAULT_WRITE_UUID, parsed["direction_code"])
        await self._device.set_speed(DEFAULT_WRITE_UUID, parsed["speed"])
        await self._device.set_brightness(DEFAULT_WRITE_UUID, parsed["brightness"])

        self._attr_is_on = parsed["is_on"]
        self._attr_brightness = parsed["brightness"]
        effect_name = CODE_TO_EFFECT.get(parsed["program_code"])
        if effect_name is not None:
            self._attr_effect = effect_name

        self.async_write_ha_state()
        await self._async_confirm_state()

    async def _async_confirm_state(self) -> None:
        """Reconcile optimistic state against the device's actual settings.

        The device doesn't push state changes on its own - update() drives
        a full subscribe/request/listen/parse round-trip against
        currentSettings. On success this invokes _handle_state_update via
        the same callback GenericBTStateSensor uses, which overwrites
        whatever we set optimistically above with real decoded values. If
        the round-trip fails, the optimistic state stands as our best
        guess rather than leaving the entity in a stale/unknown state.
        """
        try:
            await self._device.request_settings(DEFAULT_WRITE_UUID)
        except BleakError:
            _LOGGER.warning(
                "%s: could not connect to confirm state after write; showing optimistic state",
                self.entity_id,
            )
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception(
                "%s: unexpected error confirming state after write", self.entity_id
            )

# ---------------------------- Private Helpers --------------------------------
    async def _async_apply_brightness(self, brightness: int) -> None:
        """Send a brightness level (0-255) to the light."""
        await self._device.set_brightness(DEFAULT_WRITE_UUID, brightness)
        self._attr_brightness = brightness

    async def _async_apply_effect(self, effect: str) -> None:
        """Send an effect/program selection to the light."""
        code = EFFECT_CODES.get(effect)
        if code is None:
            raise ValueError(f"Unknown effect: {effect}")
        await self._device.set_effect(DEFAULT_WRITE_UUID, code)
        self._attr_effect = effect