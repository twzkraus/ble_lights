"""generic bt device"""

from uuid import UUID
from typing import Callable, Optional
import asyncio
import logging
from contextlib import AsyncExitStack, suppress

from bleak import BleakClient
from bleak.exc import BleakError

try:
    from bleak_retry_connector import establish_connection
except ImportError:  # pragma: no cover - optional dependency in tests
    establish_connection = None

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
        self._client_uses_context_manager = False

        # idle-disconnect bookkeeping
        self._idle_disconnect_seconds = idle_disconnect_seconds
        self._idle_disconnect_enabled = bool(idle_disconnect_seconds)
        self._idle_timer_handle: Optional[asyncio.TimerHandle] = None

        # entities (binary_sensor, switch, ...) register here to be notified
        # whenever connection state changes, regardless of what triggered it
        # (manual switch, idle timeout, a write/read service call, unload).
        self._listeners: list[Callable[[], None]] = []
        self._notify_uuid: Optional[str] = None
        self._read_uuid: Optional[str] = None
        self._notification_callback: Optional[Callable[[str], None]] = None
        self._state_callbacks: list[Callable[[], None]] = []
        self.last_notification_value: Optional[str] = None
        self._previous_notification_value: Optional[str] = None

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

    @property
    def previous_notification_value(self) -> Optional[str]:
        """Return the last distinct value seen before the current one."""
        return self._previous_notification_value

    async def update(self):
        if self._read_uuid is None:
            return
        try:
            data = await self.read_gatt(self._read_uuid)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Unable to read GATT characteristic on update", exc_info=True)
            return

        value = self._decode_data(data)
        self._update_notification_value(value)

    async def stop(self):
        """Called on integration unload/reload - make sure we actually let go of the connection."""
        await self.disconnect()
        self._notify_uuid = None
        self._read_uuid = None
        self._notification_callback = None
        self._state_callbacks = []

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
                if self._client_uses_context_manager:
                    await self._client_stack.aclose()
                else:
                    await self._client.disconnect()
            except BleakError:
                _LOGGER.debug("Error while disconnecting", exc_info=True)
            except AttributeError:
                _LOGGER.debug("Client does not support disconnect()", exc_info=True)
            finally:
                self._client = None
                self._client_uses_context_manager = False
        self._notify_listeners()

    async def get_client(self):
        was_connected = self.connected
        async with self._lock:
            if not self._client:
                _LOGGER.debug("Connecting")
                try:
                    if establish_connection is not None:
                        self._client = await establish_connection(
                            BleakClient,
                            self._ble_device,
                            self._ble_device.address,
                            timeout=30,
                        )
                        self._client_uses_context_manager = False
                    else:
                        self._client = await self._client_stack.enter_async_context(BleakClient(self._ble_device, timeout=30))
                        self._client_uses_context_manager = True
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

    def set_state_callback(self, callback: Callable[[], None]) -> None:
        if callback not in self._state_callbacks:
            self._state_callbacks.append(callback)

    def _update_notification_value(self, message: Optional[str]) -> None:
        if message is None or self.last_notification_value == message:
            return
        self._previous_notification_value = self.last_notification_value
        self.last_notification_value = message
        if self._notification_callback is not None:
            self._notification_callback(message)
        for state_callback in list(self._state_callbacks):
            with suppress(Exception):
                state_callback()

    def _decode_data(self, data) -> Optional[str]:
        if data is None:
            return None
        if isinstance(data, (bytes, bytearray)):
            raw_bytes = bytes(data)
            if not raw_bytes:
                return ""

            for encoding in ("utf-8", "utf-16-le", "utf-16-be", "utf-16", "latin-1"):
                try:
                    decoded = raw_bytes.decode(encoding)
                except UnicodeDecodeError:
                    continue

                if encoding in {"utf-16", "utf-16-le", "utf-16-be"}:
                    return decoded.replace("\x00", "")

                if "\x00" not in decoded:
                    return decoded

            return raw_bytes.decode("utf-8", errors="replace")
        return str(data)

    def _handle_notification(self, _sender, data) -> None:
        raw_data = data
        message = self._decode_data(data)
        _LOGGER.debug("Received notification payload=%r decoded=%r", raw_data, message)
        self._update_notification_value(message)

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

    async def subscribe_to_notify(self, target_uuid, callback=None):
        if self._notify_uuid == target_uuid and self._notification_callback == callback:
            return
        if self._notify_uuid is not None and self._notify_uuid != target_uuid:
            await self.unsubscribe_from_notify(self._notify_uuid)
        await self.get_client()
        uuid = self._to_uuid(target_uuid)
        self._notify_uuid = target_uuid
        self._read_uuid = target_uuid
        self._notification_callback = callback
        await self._client.start_notify(uuid, self._handle_notification)
        self._reset_idle_timer()

    async def unsubscribe_from_notify(self, target_uuid):
        if self._client is None or self._notify_uuid != target_uuid:
            return
        with suppress(Exception):
            await self._client.stop_notify(self._to_uuid(target_uuid))
        self._notify_uuid = None
        self._read_uuid = None
        self._notification_callback = None

    def update_from_advertisement(self, advertisement):
        pass