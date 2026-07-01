"""generic bt device"""

from uuid import UUID
from typing import Callable, Optional
import asyncio
import logging
from contextlib import AsyncExitStack

from bleak import BleakClient
from bleak.exc import BleakError

_LOGGER = logging.getLogger(__name__)

# Default seconds of inactivity before we auto-disconnect.
# 0 / None disables idle-disconnect entirely.
DEFAULT_IDLE_DISCONNECT_SECONDS = 30


class GenericBTBleakError(Exception):
    """Wrap bleak connection errors."""


class GenericBTTimeoutError(Exception):
    """Wrap bleak timeout errors."""


class GenericBTDevice:
    """Generic BT Device Class"""

    def __init__(self, ble_device, idle_disconnect_seconds: Optional[float] = DEFAULT_IDLE_DISCONNECT_SECONDS):
        self._ble_device = ble_device
        self._client: Optional[BleakClient] = None
        self._client_stack = AsyncExitStack()
        self._lock = asyncio.Lock()

        # idle-disconnect bookkeeping
        self._idle_disconnect_seconds = idle_disconnect_seconds
        self._idle_disconnect_enabled = bool(idle_disconnect_seconds)
        self._idle_timer_handle: Optional[asyncio.TimerHandle] = None

        # entities (binary_sensor, switch, ...) register here to be notified
        # whenever connection state changes, regardless of what triggered it
        # (manual switch, idle timeout, a write/read service call, unload).
        self._listeners: list[Callable[[], None]] = []
        self.last_notification_value = None

    def add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback to be invoked whenever connection state changes.

        Returns a function that removes the listener, for easy use in
        async_added_to_hass / async_will_remove_from_hass.
        """
        self._listeners.append(update_callback)

        def _remove() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return _remove

    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    async def update(self):
        pass

    async def stop(self):
        """Called on integration unload/reload - make sure we actually let go of the connection."""
        await self.disconnect()

    @property
    def connected(self):
        return self._client is not None

    @property
    def idle_disconnect_enabled(self) -> bool:
        return self._idle_disconnect_enabled

    @property
    def idle_disconnect_seconds(self) -> Optional[float]:
        return self._idle_disconnect_seconds

    def set_idle_disconnect(self, enabled: bool) -> None:
        """Manual toggle - turn idle-disconnect on/off at runtime."""
        self._idle_disconnect_enabled = enabled
        if enabled:
            self._reset_idle_timer()
        else:
            self._cancel_idle_timer()

    def set_idle_disconnect_seconds(self, seconds: Optional[float]) -> None:
        """Adjust the idle window at runtime."""
        self._idle_disconnect_seconds = seconds
        self._idle_disconnect_enabled = bool(seconds)
        if self._idle_disconnect_enabled and self.connected:
            self._reset_idle_timer()
        else:
            self._cancel_idle_timer()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer_handle is not None:
            self._idle_timer_handle.cancel()
            self._idle_timer_handle = None

    def _reset_idle_timer(self) -> None:
        """(Re)start the idle countdown. Call this after any successful activity."""
        self._cancel_idle_timer()
        if not self._idle_disconnect_enabled or not self._idle_disconnect_seconds:
            return
        loop = asyncio.get_event_loop()
        self._idle_timer_handle = loop.call_later(
            self._idle_disconnect_seconds,
            lambda: asyncio.create_task(self._idle_disconnect()),
        )

    async def _idle_disconnect(self) -> None:
        _LOGGER.debug("Idle timeout reached, disconnecting")
        await self.disconnect()

    async def disconnect(self) -> None:
        """Tear down the BLE connection now, whether idle-timeout or manual/unload triggered."""
        async with self._lock:
            self._cancel_idle_timer()
            if self._client is None:
                return
            _LOGGER.debug("Disconnecting")
            try:
                await self._client_stack.aclose()
            except BleakError:
                _LOGGER.debug("Error while disconnecting", exc_info=True)
            finally:
                self._client = None
        self._notify_listeners()

    async def get_client(self):
        was_connected = self.connected
        async with self._lock:
            if not self._client:
                _LOGGER.debug("Connecting")
                try:
                    self._client = await self._client_stack.enter_async_context(BleakClient(self._ble_device, timeout=30))
                except asyncio.TimeoutError as exc:
                    _LOGGER.debug("Timeout on connect", exc_info=True)
                    raise GenericBTTimeoutError("Timeout on connect") from exc
                except BleakError as exc:
                    _LOGGER.debug("Error on connect", exc_info=True)
                    raise GenericBTBleakError("Error on connect") from exc
            else:
                _LOGGER.debug("Connection reused")

        # Outside the lock - resetting the timer doesn't need to block on it,
        # and we don't want to deadlock if this ever gets called from within
        # the idle-disconnect callback path.
        self._reset_idle_timer()
        if not was_connected:
            self._notify_listeners()

    async def write_gatt(self, target_uuid, data):
        await self.get_client()
        uuid_str = "{" + target_uuid + "}"
        uuid = UUID(uuid_str)
        data_as_bytes = bytearray.fromhex(data)
        await self._client.write_gatt_char(uuid, data_as_bytes, True)
        self._reset_idle_timer()

    async def read_gatt(self, target_uuid):
        await self.get_client()
        uuid_str = "{" + target_uuid + "}"
        uuid = UUID(uuid_str)
        data = await self._client.read_gatt_char(uuid)
        self._reset_idle_timer()
        return data

    def _to_uuid(self, target_uuid):
        uuid_str = "{" + target_uuid + "}"
        return UUID(uuid_str)

    async def subscribe_to_notify(self, target_uuid):
        await self.get_client()
        uuid = self._to_uuid(target_uuid)

        def _callback(_sender, data):
            if data is not None:
                if isinstance(data, (bytes, bytearray)):
                    self.last_notification_value = data.decode("utf-8")
                else:
                    self.last_notification_value = str(data)

        await self._client.start_notify(uuid, _callback)
        self._reset_idle_timer()

    def update_from_advertisement(self, advertisement):
        pass