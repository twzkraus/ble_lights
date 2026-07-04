from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import GenericBTCoordinator
from .entity import GenericBTEntity
from .const import COLOR_PALETTES, COLOR_PALETTE_NAMES, DEFAULT_WRITE_UUID, DOMAIN


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
    _attr_current_option: str | None = None

    def __init__(self, coordinator: GenericBTCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.base_unique_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.palette_select_entity = self

    async def async_will_remove_from_hass(self) -> None:
        self.coordinator.palette_select_entity = None
        await super().async_will_remove_from_hass()

    async def async_select_option(self, option: str) -> None:
        colors = COLOR_PALETTES[option]
        await self._device.set_colors_hsv(DEFAULT_WRITE_UUID, colors)
        await self._async_confirm_state()
        self._attr_current_option = option
        self.async_write_ha_state()

    def invalidate_palette(self) -> None:
        """Clear the selected palette, e.g. when color is set outside select_option."""
        self._attr_current_option = None
        self.async_write_ha_state()