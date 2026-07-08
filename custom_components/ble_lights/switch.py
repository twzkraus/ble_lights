"""Support for Generic BT connection switch."""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, DEFAULT_WRITE_UUID
from .coordinator import GenericBTCoordinator
from .entity import GenericBTEntity


_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Generic BT switch based on a config entry."""
    coordinator: GenericBTCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([GenericBTConnectionSwitch(coordinator)])


class GenericBTConnectionSwitch(GenericBTEntity, SwitchEntity):
    """Manual connect/disconnect toggle for the BLE link.

    On = connected (and idle-disconnect re-armed if it was enabled).
    Off = force-disconnect now, and stay disconnected until turned back on
    or until another GATT call (write/read service) reconnects it.
    """

    _attr_name = "Connection"
    _attr_icon = "mdi:bluetooth-connect"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: GenericBTCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}_connection"

    @property
    def is_on(self) -> bool:
        return self._device.connected

    async def async_turn_on(self, **kwargs) -> None:
        """Connect now."""
        await self._device.get_client()
        await self._device.request_settings(DEFAULT_WRITE_UUID)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Disconnect now (this is the manual override)."""
        await self._device.disconnect()
        self.async_write_ha_state()