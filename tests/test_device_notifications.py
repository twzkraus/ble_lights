import importlib.util
import sys
import types
from pathlib import Path

import pytest


bleak_module = types.ModuleType("bleak")


class _BleakClient:  # pragma: no cover - simple stub for import
    def __init__(self, *args, **kwargs):
        pass


bleak_module.BleakClient = _BleakClient
sys.modules.setdefault("bleak", bleak_module)

bleak_exc_module = types.ModuleType("bleak.exc")


class _BleakError(Exception):
    pass


bleak_exc_module.BleakError = _BleakError
sys.modules.setdefault("bleak.exc", bleak_exc_module)

MODULE_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "generic_bt_twzkraus" / "generic_bt_api" / "device.py"
SPEC = importlib.util.spec_from_file_location("generic_bt_device", MODULE_PATH)
DEVICE_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(DEVICE_MODULE)
GenericBTDevice = DEVICE_MODULE.GenericBTDevice


class FakeClient:
    def __init__(self, read_value=None):
        self.notifies = {}
        self.read_value = read_value
        self.read_calls = []

    async def start_notify(self, uuid, callback):
        self.notifies[uuid] = callback

    async def stop_notify(self, uuid):
        self.notifies.pop(uuid, None)

    async def read_gatt_char(self, uuid):
        self.read_calls.append(uuid)
        return self.read_value

    async def disconnect(self):
        return None


@pytest.mark.asyncio
async def test_get_client_uses_bleak_retry_connector_when_available(monkeypatch):
    ble_device = type("BleDevice", (), {"address": "AA:BB:CC:DD:EE:FF"})()
    device = GenericBTDevice(ble_device)
    call_args = {}

    async def fake_establish_connection(client_cls, ble_device_arg, address, timeout=30):
        call_args["client_cls"] = client_cls
        call_args["ble_device"] = ble_device_arg
        call_args["address"] = address
        call_args["timeout"] = timeout
        return FakeClient()

    monkeypatch.setattr(DEVICE_MODULE, "establish_connection", fake_establish_connection)

    await device.get_client()

    assert call_args["ble_device"] is ble_device
    assert call_args["address"] == ble_device.address
    assert call_args["timeout"] == 30
    assert device.connected is True


@pytest.mark.asyncio
async def test_notification_subscription_updates_last_value():
    ble_device = type("BleDevice", (), {"address": "AA:BB:CC:DD:EE:FF"})()
    device = GenericBTDevice(ble_device)
    device._client = FakeClient()

    await device.subscribe_to_notify("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")

    assert device.last_notification_value is None

    notify_callback = device._client.notifies[device._to_uuid("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")]
    notify_callback(None, b"pattern-2")

    assert device.last_notification_value == "pattern-2"


@pytest.mark.asyncio
async def test_update_reads_initial_state_from_gatt_on_connect():
    ble_device = type("BleDevice", (), {"address": "AA:BB:CC:DD:EE:FF"})()
    device = GenericBTDevice(ble_device)
    device._client = FakeClient(read_value=b"pattern-1")

    await device.subscribe_to_notify("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")
    await device.update()

    assert device.last_notification_value == "pattern-1"
    assert device.previous_notification_value is None
    assert len(device._client.read_calls) == 1


@pytest.mark.asyncio
async def test_previous_value_tracks_last_distinct_state():
    ble_device = type("BleDevice", (), {"address": "AA:BB:CC:DD:EE:FF"})()
    device = GenericBTDevice(ble_device)
    device._client = FakeClient()

    await device.subscribe_to_notify("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")

    notify_callback = device._client.notifies[device._to_uuid("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")]
    notify_callback(None, b"pattern-1")
    notify_callback(None, b"pattern-2")
    notify_callback(None, b"pattern-2")

    assert device.last_notification_value == "pattern-2"
    assert device.previous_notification_value == "pattern-1"
