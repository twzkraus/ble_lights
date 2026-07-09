from __future__ import annotations
from typing import Callable

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import GenericBTCoordinator
from .entity import GenericBTEntity
from .const import COLOR_PALETTES, COLOR_PALETTE_NAMES, DEFAULT_WRITE_UUID, DOMAIN, NUM_COLOR_SLOTS


def _palette_to_hsv_tuples(colors: list[list[int]]) -> tuple[tuple[int, int, int], ...]:
    """Pad a palette definition out to the full 6-slot form the device reports."""
    padded = [tuple(c) for c in colors[:NUM_COLOR_SLOTS]]
    while len(padded) < NUM_COLOR_SLOTS:
        padded.append((0, 0, 0))
    return tuple(padded)


def _match_palette(colors: list[dict] | None) -> str | None:
    """Return the palette name whose colors exactly match the device's parsed colors, if any."""
    if not colors:
        return None
    current = tuple((c["hue"], c["saturation"], c["value"]) for c in colors)
    for name, palette_colors in COLOR_PALETTES.items():
        if _palette_to_hsv_tuples(palette_colors) == current:
            return name
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GenericBTCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([GenericBTSelect(coordinator)])


class GenericBTSelect(GenericBTEntity, SelectEntity):
    _attr_icon = "mdi:palette"
    _attr_options = COLOR_PALETTE_NAMES
    _attr_name = "Color Palette"

    def __init__(self, coordinator: GenericBTCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}_color_palette"
        self._attr_current_option: str | None = None
        self._remove_state_callback: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.palette_select_entity = self
        # New parsed settings (from notifications, polls, or the initial
        # post-subscribe read) fan out to entities via _state_callbacks
        self._remove_state_callback = self._device.set_state_callback(self._handle_device_state_update)
        # Reflect state the device already reported if available
        self._update_current_option_from_device()

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_state_callback is not None:
            self._remove_state_callback()
            self._remove_state_callback = None
        self.coordinator.palette_select_entity = None
        await super().async_will_remove_from_hass()

    def _handle_device_state_update(self) -> None:
        """Called whenever the device pushes freshly parsed settings data."""
        self._update_current_option_from_device()
        self.async_write_ha_state()

    def _update_current_option_from_device(self) -> None:
        data = self._device.last_notification_data
        colors = data.colors if data else None
        if colors is None:
            # No data yet (mid-reconnect blip) — keep prior state
            return
        self._attr_current_option = _match_palette(colors)

    def set_palette_option(self, option: str) -> None:
        """Optimistically set the selected palette without sending a command to the device."""
        self._attr_current_option = option
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        colors = COLOR_PALETTES[option]
        await self._device.set_colors_hsv(DEFAULT_WRITE_UUID, colors)
        await self._device.request_settings(DEFAULT_WRITE_UUID)
        self.set_palette_option(option)

    def invalidate_palette(self) -> None:
        """Clear the selected palette, e.g. when color is set outside select_option."""
        self._attr_current_option = None
        self.async_write_ha_state()