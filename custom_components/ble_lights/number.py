"""Support for Generic BT idle disconnect timeout number."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import GenericBTCoordinator
from .entity import GenericBTEntity
from .generic_bt_api.device import DEFAULT_IDLE_DISCONNECT_SECONDS


_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the idle disconnect timeout number entity."""
    coordinator: GenericBTCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([GenericBTIdleDisconnectNumber(coordinator)])


class GenericBTIdleDisconnectNumber(GenericBTEntity, NumberEntity, RestoreEntity):
    """Expose the idle disconnect timeout as a configurable HA number entity."""

    _attr_name = "Auto disconnect timeout"
    _attr_icon = "mdi:timer-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 3600
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "s"

    def __init__(self, coordinator: GenericBTCoordinator) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}_idle_disconnect_timeout"
        self._attr_native_value = float(DEFAULT_IDLE_DISCONNECT_SECONDS)

    async def async_added_to_hass(self) -> None:
        """Restore the last configured timeout and apply it to the device."""
        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = float(last_state.state)
            except ValueError:
                _LOGGER.debug("Unable to restore idle disconnect timeout from %s", last_state.state)

        self._device.set_idle_disconnect_seconds(float(self._attr_native_value))
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Return the currently configured idle disconnect timeout."""
        return self._device.idle_disconnect_seconds

    async def async_set_native_value(self, value: float) -> None:
        """Update the configured idle disconnect timeout."""
        self._device.set_idle_disconnect_seconds(float(value))
        self._attr_native_value = float(value)
        self.async_write_ha_state()
