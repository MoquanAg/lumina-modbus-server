"""Tests for the Modbus sensor scanner helpers."""

from pathlib import Path
import importlib.util

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "modbus_sensor_scanner.py"


def load_scanner():
    spec = importlib.util.spec_from_file_location("modbus_sensor_scanner", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_int_list_accepts_hex_ranges_and_csv_values():
    scanner = load_scanner()

    assert scanner.parse_int_list("0x10-0x12,0x15") == [0x10, 0x11, 0x12, 0x15]
    assert scanner.parse_int_list("9600,38400") == [9600, 38400]


def test_build_read_holding_frame_includes_modbus_crc():
    scanner = load_scanner()

    frame = scanner.build_read_holding_frame(0x10, 0x0000, 11)

    assert frame.hex() == "10030000000b074c"


def test_decode_shanheng_thc_response_with_co2():
    scanner = load_scanner()

    reading = scanner.decode_sensor_response(
        bytes.fromhex("10031600eb022b000000000000000000000000000000000320b932"),
        expected_address=0x10,
    )

    assert reading is not None
    assert reading.sensor_type == "THC"
    assert reading.vendor == "SHANHENG"
    assert reading.temperature_c == pytest.approx(23.5)
    assert reading.humidity_pct == pytest.approx(55.5)
    assert reading.co2_ppm == pytest.approx(800)


def test_decode_shanheng_th_response_without_co2():
    scanner = load_scanner()

    reading = scanner.decode_sensor_response(
        bytes.fromhex("10030400fd025aeb99"),
        expected_address=0x10,
    )

    assert reading is not None
    assert reading.sensor_type == "TH"
    assert reading.vendor == "SHANHENG"
    assert reading.temperature_c == pytest.approx(25.3)
    assert reading.humidity_pct == pytest.approx(60.2)
    assert reading.co2_ppm is None


def test_decode_wms_layout_when_registers_are_swapped():
    scanner = load_scanner()

    reading = scanner.decode_sensor_response(
        bytes.fromhex("100304025a00fd1b18"),
        expected_address=0x10,
    )

    assert reading is not None
    assert reading.vendor == "WMS"
    assert reading.temperature_c == pytest.approx(25.3)
    assert reading.humidity_pct == pytest.approx(60.2)


@pytest.mark.parametrize(
    ("response_hex", "address", "temperature_c"),
    [
        ("71030400cc00004bcb", 0x71, 20.4),
        ("72031600a60000000000a60000000000000000070800000000eec2", 0x72, 16.6),
        ("82031600ba0000000000ba00000000000000000708000000002a56", 0x82, 18.6),
    ],
)
def test_decode_shanheng_temperature_only_response_with_zero_humidity(
    response_hex, address, temperature_c
):
    scanner = load_scanner()

    reading = scanner.decode_sensor_response(
        bytes.fromhex(response_hex),
        expected_address=address,
    )

    assert reading is not None
    assert reading.sensor_type == "TEMP"
    assert reading.vendor == "SHANHENG"
    assert reading.temperature_c == pytest.approx(temperature_c)
    assert reading.humidity_pct is None
    assert reading.co2_ppm is None


def test_decode_rejects_wrong_address_and_bad_crc():
    scanner = load_scanner()

    assert scanner.decode_sensor_response(
        bytes.fromhex("11030400fd025aeb99"),
        expected_address=0x10,
    ) is None
    assert scanner.decode_sensor_response(
        bytes.fromhex("10030400fd025a0000"),
        expected_address=0x10,
    ) is None
