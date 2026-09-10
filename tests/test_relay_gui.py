"""Tests for the local relay GUI command helpers."""

from pathlib import Path
import importlib.util

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "relay_gui.py"


def load_relay_gui():
    spec = importlib.util.spec_from_file_location("relay_gui", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_write_single_coil_frames_for_zero_based_channels():
    relay_gui = load_relay_gui()

    assert relay_gui.build_write_single_coil_frame(0x10, 0, True).hex() == "10050000ff008f7b"
    assert relay_gui.build_write_single_coil_frame(0x10, 0, False).hex() == "100500000000ce8b"
    assert relay_gui.build_write_single_coil_frame(0x10, 5, True).hex() == "10050005ff009f7a"
    assert relay_gui.build_write_single_coil_frame(0x10, 15, False).hex() == "1005000f0000fe88"


def test_build_write_single_coil_frame_rejects_invalid_channels():
    relay_gui = load_relay_gui()

    with pytest.raises(ValueError, match="channel"):
        relay_gui.build_write_single_coil_frame(0x10, -1, True)

    with pytest.raises(ValueError, match="channel"):
        relay_gui.build_write_single_coil_frame(0x10, 16, False)


def test_build_modbus_server_line_includes_timeout_and_response_length():
    relay_gui = load_relay_gui()

    line = relay_gui.build_modbus_server_line(
        command_id="relay_0_on",
        port="/dev/ttyAMA1",
        baudrate=38400,
        frame=bytes.fromhex("10050000ff008f7b"),
        timeout=2.5,
    )

    assert line == "relay_0_on:relay:/dev/ttyAMA1:38400:10050000ff008f7b:8:2.5\n"


def test_default_relay_config_uses_bench_slave_address():
    relay_gui = load_relay_gui()

    config = relay_gui.RelayConfig()

    assert config.slave_address == 0x01
    assert config.as_dict()["slave_address_hex"] == "0x01"


def test_load_device_conf_uses_relay_settings_labels_and_exclusive_groups(tmp_path):
    relay_gui = load_relay_gui()
    device_conf = tmp_path / "device.conf"
    device_conf.write_text(
        """
[RELAY_CONTROL]
relayone = relay, one, "Relay Board One", /dev/ttyAMA1, 0x01, 38400

[RELAY_ASSIGNMENTS]
relay_one_0_to_3 = CoolWaterTankPumpOnOff, HotWaterTankPumpOnOff, ReheatFCUFanPower, DehumidificationFCUHigh
relay_one_4_to_7 = DehumidificationFCUMid, DehumidificationFCULow, null, null

[RELAY_EXCLUSIVE_GROUPS]
dehumidification_fcu_speed = 3, 4, 5
""".strip()
    )

    settings = relay_gui.load_device_conf_settings(device_conf, "one")

    assert settings["serial_port"] == "/dev/ttyAMA1"
    assert settings["baudrate"] == 38400
    assert settings["slave_address"] == 0x01
    assert settings["channel_labels"][0] == "Cool water tank pump on/off"
    assert settings["channel_labels"][3] == "Dehumidification FCU high"
    assert settings["channel_labels"][5] == "Dehumidification FCU low"
    assert settings["exclusive_groups"] == [[3, 4, 5]]


def test_relay_config_finds_exclusive_peer_channels():
    relay_gui = load_relay_gui()
    config = relay_gui.RelayConfig(exclusive_groups=[[3, 4, 5]])

    assert config.exclusive_peers_for(3) == [4, 5]
    assert config.exclusive_peers_for(4) == [3, 5]
    assert config.exclusive_peers_for(0) == []
