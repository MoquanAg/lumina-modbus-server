"""Tests for the Modbus sensor scanner helpers."""

from pathlib import Path
import importlib.util
import struct

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "modbus_sensor_scanner.py"


def load_scanner():
    spec = importlib.util.spec_from_file_location("modbus_sensor_scanner", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_response(scanner, address, function_code, registers):
    data = b"".join(struct.pack(">H", value & 0xFFFF) for value in registers)
    body = bytes((address, function_code, len(data))) + data
    return body + scanner.modbus_crc(body)


def test_parse_int_list_accepts_hex_ranges_and_csv_values():
    scanner = load_scanner()

    assert scanner.parse_int_list("0x10-0x12,0x15") == [0x10, 0x11, 0x12, 0x15]
    assert scanner.parse_int_list("9600,38400") == [9600, 38400]
    with pytest.raises(ValueError, match="too many values"):
        scanner.parse_int_list("0-1000000000")


def test_parse_port_list_rejects_protocol_injection_and_unknown_paths():
    scanner = load_scanner()

    assert scanner.parse_port_list("/dev/ttyAMA1,/dev/ttyAMA3") == [
        "/dev/ttyAMA1",
        "/dev/ttyAMA3",
    ]
    with pytest.raises(ValueError, match="serial port"):
        scanner.parse_port_list("/dev/ttyAMA1\ninjected:command")
    with pytest.raises(ValueError, match="serial port"):
        scanner.parse_port_list("/tmp/fake:port")

    with pytest.raises(ValueError, match="serial port"):
        scanner.build_modbus_server_line(
            command_id="scan",
            port="/dev/ttyAMA1\ninjected:command",
            baudrate=9600,
            frame=scanner.build_read_holding_frame(1, 0, 1),
            response_length=7,
            timeout=0.7,
        )


def test_build_read_holding_frame_includes_modbus_crc():
    scanner = load_scanner()

    frame = scanner.build_read_holding_frame(0x10, 0x0000, 11)

    assert frame.hex() == "10030000000b074c"


def test_build_read_frame_supports_only_read_functions():
    scanner = load_scanner()

    assert scanner.build_read_frame(0x01, 0x04, 3001, 9).hex() == "01040bb90009e3cd"
    with pytest.raises(ValueError, match="read function"):
        scanner.build_read_frame(0x01, 0x06, 0, 1)


@pytest.mark.parametrize(
    ("address", "start", "count", "message"),
    [
        (0, 0, 1, "slave_address"),
        (0xF8, 0, 1, "slave_address"),
        (1, -1, 1, "start_register"),
        (1, 0x10000, 1, "start_register"),
        (1, 0, 0, "count"),
        (1, 0, 126, "count"),
    ],
)
def test_build_read_frame_rejects_illegal_wire_boundaries(address, start, count, message):
    scanner = load_scanner()

    with pytest.raises(ValueError, match=message):
        scanner.build_read_frame(address, 0x03, start, count)


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
    scanner_response = build_response(scanner, 0x10, 0x03, [0, 0])
    ambiguous = scanner.decode_sensor_response(scanner_response, expected_address=0x10)
    assert ambiguous is not None
    assert ambiguous.sensor_type == "TEMP_AMBIGUOUS"
    assert ambiguous.temperature_c == 0.0
    assert ambiguous.details["confidence"] == "ambiguous_all_zero_payload"


def test_decode_ec092_input_registers():
    scanner = load_scanner()
    response = build_response(
        scanner,
        0x01,
        0x04,
        [1450, 0, 0, 0, 0, 310, 123, 280, 42],
    )

    reading = scanner.decode_ec092_response(response, expected_address=0x01)

    assert reading is not None
    assert reading.sensor_type == "EC092"
    assert reading.vendor == "SUNXFAN"
    assert reading.details == {
        "speed_rpm": 1450,
        "fault_code": 0,
        "limit_code": 0,
        "bus_voltage_v": 310,
        "phase_current_a": pytest.approx(1.23),
        "power_w": 280,
        "heatsink_temperature_c": 42,
    }


def test_decode_ec092_rejects_wrong_function_reserved_data_and_bad_temperature():
    scanner = load_scanner()

    assert scanner.decode_ec092_response(
        build_response(scanner, 1, 0x03, [0] * 9), 1
    ) is None
    assert scanner.decode_ec092_response(
        build_response(scanner, 1, 0x04, [0, 0, 0, 1, 0, 0, 0, 0, 20]), 1
    ) is None
    assert scanner.decode_ec092_response(
        build_response(scanner, 1, 0x04, [0, 0, 0, 0, 0, 0, 0, 0, 250]), 1
    ) is None
    exception_body = bytes((1, 0x84, 0x02))
    exception = exception_body + scanner.modbus_crc(exception_body)
    assert scanner.decode_ec092_response(exception, 1) is None
    assert scanner.decode_ec092_response(
        build_response(scanner, 1, 0x04, [0] * 9), 1
    ) is None
    assert scanner.decode_ec092_response(
        build_response(scanner, 1, 0x04, [1] * 8), 1
    ) is None
    valid = build_response(scanner, 1, 0x04, [1, 0, 0, 0, 0, 300, 1, 1, 20])
    assert scanner.decode_ec092_response(valid[:-1], 1) is None
    assert scanner.decode_ec092_response(
        build_response(scanner, 1, 0x04, [1, 0, 0xFFFF, 0, 0, 300, 1, 1, 20]), 1
    ) is None
    assert scanner.decode_ec092_response(
        build_response(scanner, 1, 0x04, [1, 0, 0, 0, 0, 300, 1, 10001, 20]), 1
    ) is None


def test_decode_adl_electricity_meter_realtime_registers():
    scanner = load_scanner()
    response = build_response(
        scanner,
        147,
        0x03,
        [2301, 234, -125, 20, 130, 950, 5000],
    )

    reading = scanner.decode_adl_meter_response(response, expected_address=147)

    assert reading is not None
    assert reading.sensor_type == "ELECTRICITY_METER"
    assert reading.vendor == "ADL"
    assert reading.details["voltage_v"] == pytest.approx(230.1)
    assert reading.details["current_a"] == pytest.approx(2.34)
    assert reading.details["active_power_kw"] == pytest.approx(-0.125)
    assert reading.details["reactive_power_kvar"] == pytest.approx(0.020)
    assert reading.details["apparent_power_kva"] == pytest.approx(0.130)
    assert reading.details["power_factor"] == pytest.approx(0.950)
    assert reading.details["frequency_hz"] == pytest.approx(50.0)


def test_decode_adl_meter_rejects_implausible_values_and_wrong_address():
    scanner = load_scanner()
    bad_power_factor = build_response(
        scanner,
        147,
        0x03,
        [2301, 234, 100, 20, 130, 1500, 5000],
    )

    assert scanner.decode_adl_meter_response(bad_power_factor, 147) is None
    assert scanner.decode_adl_meter_response(
        build_response(scanner, 147, 0x03, [7000, 0, 0, 0, 0, 0, 5000]), 147
    ) is None
    assert scanner.decode_adl_meter_response(
        build_response(scanner, 194, 0x03, [2301, 0, 0, 0, 0, 0, 5000]), 147
    ) is None
    assert scanner.decode_adl_meter_response(
        build_response(scanner, 147, 0x03, [0] * 7), 147
    ) is None
    assert scanner.decode_adl_meter_response(
        build_response(scanner, 147, 0x03, [2301] * 6), 147
    ) is None
    valid = build_response(scanner, 147, 0x03, [2301, 1, 1, 1, 1, 950, 5000])
    assert scanner.decode_adl_meter_response(valid[:-2], 147) is None
    assert scanner.decode_adl_meter_response(
        build_response(scanner, 147, 0x03, [2301, 1, 1, 1, 0xFFFF, 950, 5000]), 147
    ) is None


def test_profile_probe_specs_are_read_only_and_preserve_ec092_base_order():
    scanner = load_scanner()

    assert scanner.profile_probe_specs("th", [3001, 3000, 0]) == [
        (0x03, 0x0000, 11),
        (0x03, 0x0000, 2),
    ]
    assert scanner.profile_probe_specs("ec092", [3001, 3000, 0]) == [
        (0x04, 3001, 9),
        (0x04, 3000, 9),
        (0x04, 0, 9),
    ]
    assert scanner.profile_probe_specs("meter", [3001, 3000, 0]) == [
        (0x03, 0x000B, 7),
    ]
    assert all(
        function in {0x03, 0x04}
        for profile in ("th", "ec092", "meter")
        for function, _, _ in scanner.profile_probe_specs(profile, [3001, 3000, 0])
    )


def test_profile_addresses_pin_meters_and_validate_profiles():
    scanner = load_scanner()

    assert scanner.profile_addresses("meter", [1, 147, 194]) == [147, 194]
    assert scanner.profile_addresses("meter", [147]) == [147]
    assert scanner.profile_addresses("meter", [1, 2]) == []
    assert scanner.profile_addresses("th", [1, 2]) == [1, 2]
    with pytest.raises(ValueError, match="unknown profile"):
        scanner.profile_probe_specs("relay", [3001])
    assert scanner.parse_profile_list("th,meter,th") == ["th", "meter"]
    with pytest.raises(ValueError, match="at least one profile"):
        scanner.parse_profile_list(" , ")


def test_cli_targeted_scan_skips_meter_profile_without_aborting(monkeypatch, capsys):
    scanner = load_scanner()
    calls = []

    monkeypatch.setattr(
        scanner,
        "scan_devices",
        lambda **kwargs: calls.append(kwargs) or [],
    )

    assert scanner.main(["--addresses", "0x10-0x15", "--json"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "[]\n"
    assert "Skipping meter profile" in captured.err
    assert calls[0]["addresses"] == list(range(0x10, 0x16))


def test_cli_defaults_to_bounded_known_address_discovery():
    scanner = load_scanner()

    args = scanner.build_parser().parse_args([])

    assert args.addresses == "0x01,0x10-0x15,0x70,0x71,0x72,0x80,0x82,0x93,0xC2"
    assert args.profiles == "th,ec092,meter"
    assert args.fan_register_bases == "3001,3000,0"
    assert args.timeout == pytest.approx(0.7)
    assert args.full is False


def test_validate_scan_inputs_rejects_invalid_addresses_and_register_bases():
    scanner = load_scanner()

    scanner.validate_scan_inputs([1, 0xF7], [0, 3001, 0xFFFF])
    with pytest.raises(ValueError, match="address"):
        scanner.validate_scan_inputs([0], [3001])
    with pytest.raises(ValueError, match="address"):
        scanner.validate_scan_inputs([0xF8], [3001])
    with pytest.raises(ValueError, match="register base"):
        scanner.validate_scan_inputs([1], [-1])
    with pytest.raises(ValueError, match="register base"):
        scanner.validate_scan_inputs([1], [0x10000])


@pytest.mark.parametrize("baudrate", [0, -9600, 12345, 4_000_001])
def test_validate_baudrates_rejects_invalid_values(baudrate):
    scanner = load_scanner()

    with pytest.raises(ValueError, match="baudrate"):
        scanner.validate_baudrates([baudrate])

    scanner.validate_baudrates([4800, 9600, 38400, 115200])


@pytest.mark.parametrize("timeout", [0, -0.1, float("nan"), float("inf"), 10.1])
def test_validate_timeout_rejects_non_finite_and_unbounded_values(timeout):
    scanner = load_scanner()

    with pytest.raises(ValueError, match="timeout"):
        scanner.validate_timeout(timeout)

    scanner.validate_timeout(0.1)
    scanner.validate_timeout(10.0)


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_validate_modbus_port_rejects_out_of_range_values(port):
    scanner = load_scanner()

    with pytest.raises(ValueError, match="modbus port"):
        scanner.validate_modbus_port(port)

    scanner.validate_modbus_port(1)
    scanner.validate_modbus_port(65535)


def test_ec092_scan_preserves_candidates_from_all_register_bases(monkeypatch):
    scanner = load_scanner()
    starts = []

    def fake_probe(**kwargs):
        starts.append(kwargs["start_register"])
        reading = None
        if kwargs["start_register"] == 3000:
            reading = scanner.SensorReading(
                sensor_type="EC092",
                vendor="SUNXFAN",
                temperature_c=None,
                humidity_pct=None,
                co2_ppm=None,
                raw_hex="",
            )
        return scanner.ScanResult(
            port=kwargs["port"],
            baudrate=kwargs["baudrate"],
            address=kwargs["address"],
            probe_registers=kwargs["register_count"],
            reading=reading,
            status="CANDIDATE" if reading else "MISS",
        )

    monkeypatch.setattr(scanner, "probe_profile_address", fake_probe)

    results = scanner.scan_profile(
        profile="ec092",
        ports=["/dev/ttyAMA1"],
        baudrates=[9600],
        addresses=[1],
        fan_register_bases=[3001, 3000, 0],
        modbus_host="127.0.0.1",
        modbus_port=8888,
        timeout=0.1,
    )

    assert starts == [3001, 3000, 0]
    assert len(results) == 1
    assert results[0].status == "CANDIDATE"


def test_profile_probe_marks_ec092_and_meter_matches_as_candidates(monkeypatch):
    scanner = load_scanner()
    responses = {
        "ec092": build_response(scanner, 1, 0x04, [1450, 0, 0, 0, 0, 310, 123, 280, 42]),
        "meter": build_response(scanner, 147, 0x03, [2301, 234, 1, 1, 1, 950, 5000]),
    }

    for profile, address, function_code, start, count in (
        ("ec092", 1, 0x04, 3001, 9),
        ("meter", 147, 0x03, 0x000B, 7),
    ):
        monkeypatch.setattr(
            scanner,
            "send_modbus_command_line",
            lambda **kwargs: (responses[profile], ""),
        )
        result = scanner.probe_profile_address(
            profile=profile,
            port="/dev/ttyAMA1",
            baudrate=9600,
            address=address,
            function_code=function_code,
            start_register=start,
            register_count=count,
            modbus_host="127.0.0.1",
            modbus_port=8888,
            timeout=0.7,
        )
        assert result.status == "CANDIDATE"
        assert result.found is False


def test_parse_server_response_preserves_full_modbus_exception_text():
    scanner = load_scanner()

    response, error = scanner.parse_server_response(
        "scan-1",
        "scan-1:ERROR:Modbus exception: Illegal Data Address:1720000000.125000\n",
    )

    assert response is None
    assert error == "Modbus exception: Illegal Data Address"


def test_parse_server_response_rejects_malformed_success_suffix():
    scanner = load_scanner()
    raw = build_response(scanner, 1, 0x03, [207, 650]).hex()

    response, error = scanner.parse_server_response(
        "scan-1",
        f"scan-1:{raw}:TRAILING_GARBAGE\n",
    )

    assert response is None
    assert error == "invalid response timestamp"


def test_error_status_retains_protocol_and_server_failures():
    scanner = load_scanner()

    assert scanner._error_status("Modbus exception: Illegal Data Address") == "EXCEPTION"
    assert scanner._error_status("Incomplete response after 0.70s: 0/9 bytes") == "MISS"
    assert scanner._error_status("No response after clearing garbage data") == "MISS"
    assert scanner._error_status("response was not hex") == "ERROR"
    assert scanner._error_status("no response from modbus server") == "ERROR"


def test_scan_profile_preserves_unknown_responses_without_include_misses(monkeypatch):
    scanner = load_scanner()

    def fake_probe(**kwargs):
        return scanner.ScanResult(
            port=kwargs["port"],
            baudrate=kwargs["baudrate"],
            address=kwargs["address"],
            probe_registers=kwargs["register_count"],
            reading=None,
            status="UNKNOWN",
            note="crc-valid response with unmatched profile",
        )

    monkeypatch.setattr(scanner, "probe_profile_address", fake_probe)

    results = scanner.scan_profile(
        profile="ec092",
        ports=["/dev/ttyAMA1"],
        baudrates=[9600],
        addresses=[1],
        fan_register_bases=[3001, 3000, 0],
        modbus_host="127.0.0.1",
        modbus_port=8888,
        timeout=0.7,
        include_misses=False,
    )

    assert len(results) == 3
    assert all(result.status == "UNKNOWN" for result in results)


def test_probe_and_scan_preserve_modbus_exceptions_without_include_misses(monkeypatch):
    scanner = load_scanner()

    monkeypatch.setattr(
        scanner,
        "send_modbus_command_line",
        lambda **kwargs: (None, "Modbus exception: Illegal Data Address"),
    )
    direct = scanner.probe_profile_address(
        profile="ec092",
        port="/dev/ttyAMA1",
        baudrate=9600,
        address=1,
        function_code=0x04,
        start_register=3001,
        register_count=9,
        modbus_host="127.0.0.1",
        modbus_port=8888,
        timeout=0.7,
    )
    assert direct.status == "EXCEPTION"
    assert direct.as_dict()["probe_function"] == "0x04"
    assert direct.as_dict()["start_register"] == 3001

    results = scanner.scan_profile(
        profile="ec092",
        ports=["/dev/ttyAMA1"],
        baudrates=[9600],
        addresses=[1],
        fan_register_bases=[3001, 3000, 0],
        modbus_host="127.0.0.1",
        modbus_port=8888,
        timeout=0.7,
        include_misses=False,
    )
    assert len(results) == 3
    assert all(result.status == "EXCEPTION" for result in results)
    assert [result.as_dict()["start_register"] for result in results] == [3001, 3000, 0]


def test_probe_marks_all_zero_temperature_payload_as_candidate(monkeypatch):
    scanner = load_scanner()
    raw = build_response(scanner, 0x10, 0x03, [0, 0])
    monkeypatch.setattr(
        scanner,
        "send_modbus_command_line",
        lambda **kwargs: (raw, ""),
    )

    result = scanner.probe_address(
        port="/dev/ttyAMA1",
        baudrate=9600,
        address=0x10,
        register_count=2,
        modbus_host="127.0.0.1",
        modbus_port=8888,
        timeout=0.7,
    )

    assert result.status == "CANDIDATE"
    assert result.found is False
    assert result.reading.sensor_type == "TEMP_AMBIGUOUS"


def test_probe_marks_identityless_th_profile_match_as_candidate(monkeypatch):
    scanner = load_scanner()
    raw = build_response(scanner, 0x71, 0x03, [207, 650])
    monkeypatch.setattr(
        scanner,
        "send_modbus_command_line",
        lambda **kwargs: (raw, ""),
    )

    result = scanner.probe_address(
        port="/dev/ttyAMA1",
        baudrate=9600,
        address=0x71,
        register_count=2,
        modbus_host="127.0.0.1",
        modbus_port=8888,
        timeout=0.7,
    )

    assert result.reading.sensor_type == "TH"
    assert result.status == "CANDIDATE"
    assert result.found is False


def test_probe_rejects_th_reply_with_wrong_requested_register_count(monkeypatch):
    scanner = load_scanner()
    short_reply = build_response(scanner, 0x71, 0x03, [207, 650])
    monkeypatch.setattr(
        scanner,
        "send_modbus_command_line",
        lambda **kwargs: (short_reply, ""),
    )

    result = scanner.probe_address(
        port="/dev/ttyAMA1",
        baudrate=9600,
        address=0x71,
        register_count=11,
        modbus_host="127.0.0.1",
        modbus_port=8888,
        timeout=0.7,
    )

    assert result.reading is None
    assert result.status == "UNKNOWN"
    assert "expected 11 registers" in result.note


def test_probe_preserves_raw_modbus_exception_frame(monkeypatch):
    scanner = load_scanner()
    body = bytes((1, 0x84, 0x02))
    raw_exception = body + scanner.modbus_crc(body)
    monkeypatch.setattr(
        scanner,
        "send_modbus_command_line",
        lambda **kwargs: (raw_exception, ""),
    )

    result = scanner.probe_profile_address(
        profile="ec092",
        port="/dev/ttyAMA1",
        baudrate=9600,
        address=1,
        function_code=0x04,
        start_register=3001,
        register_count=9,
        modbus_host="127.0.0.1",
        modbus_port=8888,
        timeout=0.7,
    )

    assert result.status == "EXCEPTION"
    assert "code=0x02" in result.note
