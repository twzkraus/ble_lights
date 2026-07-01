import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace


def _load_device_module():
    """Load the device module without importing the Home Assistant integration package."""
    bleak_module = types.ModuleType("bleak")
    bleak_module.BleakClient = object

    bleak_exc_module = types.ModuleType("bleak.exc")

    class FakeBleakError(Exception):
        """Minimal stand-in for bleak errors in unit tests."""

    bleak_exc_module.BleakError = FakeBleakError

    sys.modules.setdefault("bleak", bleak_module)
    sys.modules.setdefault("bleak.exc", bleak_exc_module)

    module_path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "generic_bt"
        / "generic_bt_api"
        / "device.py"
    )
    spec = importlib.util.spec_from_file_location("generic_bt_device_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


GenericBTDevice = _load_device_module().GenericBTDevice


def test_set_idle_disconnect_seconds_updates_device() -> None:
    """The device should accept runtime updates to the idle disconnect interval."""
    device = GenericBTDevice(SimpleNamespace())

    assert device.idle_disconnect_seconds == 30

    device.set_idle_disconnect_seconds(45)

    assert device.idle_disconnect_seconds == 45
    assert device.idle_disconnect_enabled is True
