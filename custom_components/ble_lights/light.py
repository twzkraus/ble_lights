"""Support for Generic BT light."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

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
from bleak.exc import BleakError

from .const import DEFAULT_WRITE_UUID, DOMAIN
from .coordinator import GenericBTCoordinator
from .entity import GenericBTEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# --- Protocol -----------------------------------------------------------

DIRECTIONS: list[tuple[str, int]] = [
    ("Left", 0),
    ("Center", 1),
    ("Right", 2),
]
DIRECTION_CODES: dict[str, int] = dict(DIRECTIONS)

VALID_COLOR_COUNTS = (1, 2, 3, 4, 5, 6)

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

SET_COLORS_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Optional(f"color_{i}"): vol.All(
            vol.ExactSequence([cv.byte, cv.byte, cv.byte]), vol.Coerce(tuple)
        )
        for i in range(1, NUM_COLOR_SLOTS + 1)
    }
)

SET_EFFECT_SCHEMA = cv.make_entity_service_schema(
    {vol.Required("effect"): vol.In([name for name, _code in EFFECTS])}
)

SET_SPEED_SCHEMA = cv.make_entity_service_schema(
    {vol.Required("speed"): cv.byte}
)

SET_BRIGHTNESS_SCHEMA = cv.make_entity_service_schema(
    {vol.Required("brightness"): cv.byte}
)

SET_DIRECTION_SCHEMA = cv.make_entity_service_schema(
    {vol.Required("direction"): vol.In([name for name, _code in DIRECTIONS])}
)

# Attributes decoded from device notifications that belong on the light,
# as opposed to purely diagnostic fields (timer1/timer2/version/sync_mode)
# which stay on GenericBTStateSensor only.
LIGHT_RELEVANT_ATTRS = ("colors", "speed", "direction")

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
    platform.async_register_entity_service(
        "set_effect", SET_EFFECT_SCHEMA, "async_set_effect"
    )
    platform.async_register_entity_service(
        "set_speed", SET_SPEED_SCHEMA, "async_set_speed"
    )
    platform.async_register_entity_service(
        "set_brightness", SET_BRIGHTNESS_SCHEMA, "async_set_brightness"
    )
    platform.async_register_entity_service(
        "set_direction", SET_DIRECTION_SCHEMA, "async_set_direction"
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

    async def async_set_colors(self, **kwargs: Any) -> None:
        """Entity service: set 1-6 colors (color_1..color_6)."""
        colors = [
            kwargs[f"color_{i}"]
            for i in range(1, NUM_COLOR_SLOTS + 1)
            if f"color_{i}" in kwargs
        ]
        if len(colors) not in VALID_COLOR_COUNTS:
            raise ValueError(
                f"Provide 1-6 colors (color_1..color_6), got {len(colors)}"
            )

        await self._device.set_colors(DEFAULT_WRITE_UUID, colors)
        await self._async_confirm_state()

    async def async_set_effect(self, effect: str) -> None:
        """Entity service: set the light's effect/program."""
        await self._async_apply_effect(effect)
        self.async_write_ha_state()
        await self._async_confirm_state()

    async def async_set_speed(self, speed: int) -> None:
        """Entity service: set the effect speed (0-255)."""
        await self._device.set_speed(DEFAULT_WRITE_UUID, speed)
        await self._async_confirm_state()

    async def async_set_brightness(self, brightness: int) -> None:
        """Entity service: set brightness (0-255) directly."""
        await self._async_apply_brightness(brightness)
        self.async_write_ha_state()
        await self._async_confirm_state()

    async def async_set_direction(self, direction: str) -> None:
        """Entity service: set the effect direction (left/center/right)."""
        code = DIRECTION_CODES.get(direction)
        if code is None:
            raise ValueError(f"Unknown direction: {direction}")
        await self._device.set_direction(DEFAULT_WRITE_UUID, code)
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
            await self._device.update()
        except BleakError:
            _LOGGER.warning(
                "%s: could not connect to confirm state after write; showing optimistic state",
                self.entity_id,
            )
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception(
                "%s: unexpected error confirming state after write", self.entity_id
            )

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