import inspect
from watchit_core.db import Database


def test_fetch_devices_never_selects_token_hash():
    src = inspect.getsource(Database.fetch_devices)
    assert "token_hash" not in src
    # scoped by household and child
    assert "household_id" in src and "child_id" in src


def test_device_pause_methods_are_household_scoped():
    for name in ("set_device_pause", "clear_device_pause"):
        src = inspect.getsource(getattr(Database, name))
        assert "household_id" in src, f"{name} must scope by household_id"
