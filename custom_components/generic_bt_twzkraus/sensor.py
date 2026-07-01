"""Support for Generic BT notification sensors."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import GenericBTCoordinator
from .entity import GenericBTEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Generic BT notification sensors based on a config entry."""
    coordinator: GenericBTCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        GenericBTNotificationSensor(coordinator),
        GenericBTPreviousNotificationSensor(coordinator),
    ])


class GenericBTNotificationSensor(GenericBTEntity, SensorEntity, RestoreEntity):
    """Representation of the last notification value reported by the device."""

    _attr_name = "notification value"
    _attr_should_poll = False

    def __init__(self, coordinator: GenericBTCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}_notification"
        self._attr_native_value: str | None = None
        self._device.set_state_callback(self._handle_state_update)

    async def async_added_to_hass(self) -> None:
        """Restore the last known value when the entity is added."""
        await super().async_added_to_hass()
        if self._device.last_notification_value is not None:
            self._attr_native_value = self._device.last_notification_value

        restored_state = await self.async_get_last_state()
        if restored_state is not None and restored_state.state not in (None, "unknown", "unavailable"):
            self._attr_native_value = restored_state.state

        self.async_write_ha_state()

    @callback
    def _handle_state_update(self) -> None:
        """Refresh the entity state when the device pushes a new notification."""
        self._attr_native_value = self._device.last_notification_value
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        """Return the current notification value."""
        return self._attr_native_value


class GenericBTPreviousNotificationSensor(GenericBTEntity, SensorEntity, RestoreEntity):
    """Representation of the previous distinct notification value reported by the device."""

    _attr_name = "previous notification value"
    _attr_should_poll = False

    def __init__(self, coordinator: GenericBTCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}_previous_notification"
        self._attr_native_value: str | None = None
        self._device.set_state_callback(self._handle_state_update)

    async def async_added_to_hass(self) -> None:
        """Restore the last known previous value when the entity is added."""
        await super().async_added_to_hass()
        if self._device.previous_notification_value is not None:
            self._attr_native_value = self._device.previous_notification_value

        restored_state = await self.async_get_last_state()
        if restored_state is not None and restored_state.state not in (None, "unknown", "unavailable"):
            self._attr_native_value = restored_state.state

        self.async_write_ha_state()

    @callback
    def _handle_state_update(self) -> None:
        """Refresh the entity state when the device pushes a new notification."""
        self._attr_native_value = self._device.previous_notification_value
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        """Return the previous notification value."""
        return self._attr_native_value
