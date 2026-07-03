"""An abstract class common to all Generic BT entities."""
from __future__ import annotations

import logging
from typing import Callable

from homeassistant.components.bluetooth.passive_update_coordinator import PassiveBluetoothCoordinatorEntity
from homeassistant.helpers import device_registry as dr

from .coordinator import GenericBTCoordinator

_LOGGER = logging.getLogger(__name__)

class GenericBTEntity(PassiveBluetoothCoordinatorEntity[GenericBTCoordinator]):
    """Generic entity encapsulating common features of Generic BT device."""

    _device: GenericBTDevice
    _attr_has_entity_name = True

    def __init__(self, coordinator: GenericBTCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._device = coordinator.device
        self._address = coordinator.ble_device.address
        self._attr_unique_id = coordinator.base_unique_id
        self._attr_device_info = {
            "connections":{(dr.CONNECTION_BLUETOOTH, self._address)},
            "name":coordinator.device_name
        }
        self._remove_device_listener: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Register for connection-state updates from the device, in addition to coordinator updates."""
        await super().async_added_to_hass()
        self._remove_device_listener = self._device.add_listener(self._handle_device_update)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister the connection-state listener."""
        if self._remove_device_listener is not None:
            self._remove_device_listener()
            self._remove_device_listener = None
        await super().async_will_remove_from_hass()

    def _handle_device_update(self) -> None:
        """Called whenever the device's connection state changes, regardless of trigger."""
        self.async_write_ha_state()