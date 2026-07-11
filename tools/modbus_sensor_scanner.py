#!/usr/bin/env python3
"""Scan RS-485 ports for Lumina Modbus sensors and equipment.

The scanner talks to the local lumina-modbus-server TCP protocol. It sends only
Modbus function 0x03/0x04 reads and never writes to the bus.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import socket
import struct
import sys
import time
from typing import Any


CHANNEL_READ_TIMEOUT_SECONDS = 0.7
DEFAULT_MODBUS_HOST = "127.0.0.1"
DEFAULT_MODBUS_PORT = 8888
DEFAULT_PORTS = ("/dev/ttyAMA1", "/dev/ttyAMA2", "/dev/ttyAMA3")
DEFAULT_BAUDRATES = (9600,)
DEFAULT_ADDRESSES = tuple(range(0x01, 0xF8))
DEFAULT_ADDRESS_TEXT = "0x01,0x10-0x15,0x70,0x71,0x72,0x80,0x82,0x93,0xC2"
DEFAULT_PROFILES = ("th", "ec092", "meter")
DEFAULT_FAN_REGISTER_BASES = (3001, 3000, 0)
METER_ADDRESSES = (147, 194)
SUPPORTED_BAUDRATES = frozenset({1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200})
MAX_PARSED_VALUES = 1024

EXPECTED_ADDRESS_LABELS = {
    0x10: "Supply end lower",
    0x11: "Supply end upper",
    0x12: "Return end upper",
    0x13: "Return end lower",
    0x14: "Middle third layer",
    0x15: "Outdoor",
    0x70: "Tank 1 cooling/dehumidification coil return water",
    0x80: "Tank 1 reheat coil return water",
    147: "Electricity meter 147",
    194: "Electricity meter 194",
}


class SensorReading:
    def __init__(
        self,
        *,
        sensor_type: str,
        vendor: str,
        temperature_c: float | None,
        humidity_pct: float | None,
        co2_ppm: float | None,
        raw_hex: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.sensor_type = sensor_type
        self.vendor = vendor
        self.temperature_c = temperature_c
        self.humidity_pct = humidity_pct
        self.co2_ppm = co2_ppm
        self.raw_hex = raw_hex
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        result = {
            "sensor_type": self.sensor_type,
            "vendor": self.vendor,
            "temperature_c": None if self.temperature_c is None else round(self.temperature_c, 2),
            "humidity_pct": None if self.humidity_pct is None else round(self.humidity_pct, 2),
            "co2_ppm": None if self.co2_ppm is None else round(self.co2_ppm, 1),
            "raw_hex": self.raw_hex,
        }
        result.update(self.details)
        return result


class ScanResult:
    def __init__(
        self,
        *,
        port: str,
        baudrate: int,
        address: int,
        probe_registers: int,
        reading: SensorReading | None,
        status: str,
        note: str = "",
        expected_label: str | None = None,
        probe_function: int | None = None,
        start_register: int | None = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.address = address
        self.probe_registers = probe_registers
        self.reading = reading
        self.status = status
        self.note = note
        self.expected_label = expected_label
        self.probe_function = probe_function
        self.start_register = start_register

    @property
    def found(self) -> bool:
        return self.reading is not None and self.status == "FOUND"

    def as_dict(self) -> dict[str, Any]:
        base = {
            "status": self.status,
            "port": self.port,
            "baudrate": self.baudrate,
            "address": self.address,
            "address_hex": f"0x{self.address:02X}",
            "expected_label": self.expected_label,
            "probe_registers": self.probe_registers,
            "probe_function": (
                None if self.probe_function is None else f"0x{self.probe_function:02X}"
            ),
            "start_register": self.start_register,
            "note": self.note,
        }
        if self.reading:
            base.update(self.reading.as_dict())
        return base


def parse_int_list(value: str) -> list[int]:
    items: list[int] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue

        if "-" in chunk:
            start_text, end_text = chunk.split("-", 1)
            start = int(start_text.strip(), 0)
            end = int(end_text.strip(), 0)
            if end < start:
                raise ValueError(f"invalid descending range: {chunk}")
            if end - start + 1 > MAX_PARSED_VALUES - len(items):
                raise ValueError(f"too many values; maximum is {MAX_PARSED_VALUES}")
            items.extend(range(start, end + 1))
        else:
            if len(items) >= MAX_PARSED_VALUES:
                raise ValueError(f"too many values; maximum is {MAX_PARSED_VALUES}")
            items.append(int(chunk, 0))

    deduped: list[int] = []
    for item in items:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _validate_serial_port(port: str) -> None:
    if re.fullmatch(r"/dev/ttyAMA[0-9]+", port) is None:
        raise ValueError("each serial port must match /dev/ttyAMA<number>")


def parse_port_list(value: str) -> list[str]:
    ports = [item.strip() for item in value.split(",") if item.strip()]
    if not ports:
        raise ValueError("at least one port is required")
    for port in ports:
        _validate_serial_port(port)
    return ports


def parse_profile_list(value: str) -> list[str]:
    profiles = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not profiles:
        raise ValueError("at least one profile is required")
    for profile in profiles:
        if profile not in DEFAULT_PROFILES:
            raise ValueError(f"unknown profile: {profile}")
    return list(dict.fromkeys(profiles))


def profile_probe_specs(
    profile: str,
    fan_register_bases: list[int],
) -> list[tuple[int, int, int]]:
    if profile == "th":
        return [(0x03, 0x0000, 11), (0x03, 0x0000, 2)]
    if profile == "ec092":
        return [(0x04, base, 9) for base in fan_register_bases]
    if profile == "meter":
        return [(0x03, 0x000B, 7)]
    raise ValueError(f"unknown profile: {profile}")


def profile_addresses(profile: str, addresses: list[int]) -> list[int]:
    if profile == "meter":
        return [address for address in addresses if address in METER_ADDRESSES]
    if profile in ("th", "ec092"):
        return addresses
    raise ValueError(f"unknown profile: {profile}")


def validate_scan_inputs(addresses: list[int], fan_register_bases: list[int]) -> None:
    if not addresses:
        raise ValueError("at least one address is required")
    if any(address < 1 or address > 0xF7 for address in addresses):
        raise ValueError("each address must be between 0x01 and 0xF7")
    if not fan_register_bases:
        raise ValueError("at least one fan register base is required")
    if any(base < 0 or base > 0xFFFF for base in fan_register_bases):
        raise ValueError("each fan register base must fit in uint16")


def validate_timeout(timeout: float) -> None:
    if not math.isfinite(timeout) or not 0.0 < timeout <= 10.0:
        raise ValueError("timeout must be finite and between 0 and 10 seconds")


def validate_modbus_port(port: int) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("modbus port must be between 1 and 65535")


def validate_baudrates(baudrates: list[int]) -> None:
    if not baudrates or any(baudrate not in SUPPORTED_BAUDRATES for baudrate in baudrates):
        allowed = ", ".join(str(value) for value in sorted(SUPPORTED_BAUDRATES))
        raise ValueError(f"each baudrate must be one of: {allowed}")


def modbus_crc(payload: bytes) -> bytes:
    crc = 0xFFFF
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def has_valid_crc(frame: bytes) -> bool:
    if len(frame) < 5:
        return False
    return modbus_crc(frame[:-2]) == frame[-2:]


def build_read_frame(
    slave_address: int,
    function_code: int,
    start_register: int,
    count: int,
) -> bytes:
    if not 1 <= slave_address <= 0xF7:
        raise ValueError("slave_address must be between 0x01 and 0xF7")
    if function_code not in (0x03, 0x04):
        raise ValueError("function_code must be a read function (0x03 or 0x04)")
    if not 0 <= start_register <= 0xFFFF:
        raise ValueError("start_register must fit in uint16")
    if not 1 <= count <= 125:
        raise ValueError("count must be between 1 and 125")

    body = struct.pack(">BBHH", slave_address, function_code, start_register, count)
    return body + modbus_crc(body)


def build_read_holding_frame(slave_address: int, start_register: int, count: int) -> bytes:
    return build_read_frame(slave_address, 0x03, start_register, count)


def build_modbus_server_line(
    *,
    command_id: str,
    port: str,
    baudrate: int,
    frame: bytes,
    response_length: int,
    timeout: float,
) -> str:
    _validate_serial_port(port)
    return (
        f"{command_id}:sensor_scan:{port}:{baudrate}:"
        f"{frame.hex()}:{response_length}:{timeout}\n"
    )


def _parse_registers(response: bytes, expected_function: int = 0x03) -> list[int] | None:
    if len(response) < 7:
        return None
    if response[1] != expected_function:
        return None
    byte_count = response[2]
    if byte_count % 2 != 0:
        return None
    if len(response) != 3 + byte_count + 2:
        return None
    data = response[3 : 3 + byte_count]
    return [struct.unpack(">H", data[index : index + 2])[0] for index in range(0, len(data), 2)]


def _signed16(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def _candidate_score(temperature_c: float, humidity_pct: float) -> int:
    if not (-50 < temperature_c < 100 and 0 < humidity_pct <= 100):
        return -1

    score = 0
    if -20 <= temperature_c <= 50:
        score += 4
    elif -30 <= temperature_c <= 60:
        score += 2
    if 10 <= humidity_pct <= 95:
        score += 3
    elif 1 <= humidity_pct <= 100:
        score += 1
    if 0 <= temperature_c <= 45 and 20 <= humidity_pct <= 90:
        score += 2
    return score


def _decode_candidate(registers: list[int], vendor: str, raw_hex: str) -> SensorReading | None:
    if len(registers) < 2:
        return None

    if vendor == "SHANHENG":
        temp_raw = _signed16(registers[0])
        humidity_raw = registers[1]
    else:
        humidity_raw = registers[0]
        temp_raw = _signed16(registers[1])

    temperature_c = temp_raw * 0.1
    humidity_pct = humidity_raw * 0.1
    if _candidate_score(temperature_c, humidity_pct) < 0:
        return None

    co2_ppm: float | None = None
    if len(registers) >= 11:
        co2_raw = registers[10]
        if 100 < co2_raw <= 5000:
            co2_ppm = float(co2_raw)

    return SensorReading(
        sensor_type="THC" if co2_ppm is not None else "TH",
        vendor=vendor,
        temperature_c=temperature_c,
        humidity_pct=humidity_pct,
        co2_ppm=co2_ppm,
        raw_hex=raw_hex,
    )


def decode_sensor_response(response: bytes, expected_address: int) -> SensorReading | None:
    if len(response) < 5:
        return None
    if response[0] != expected_address:
        return None
    if not has_valid_crc(response):
        return None
    if response[1] & 0x80:
        return None

    registers = _parse_registers(response, 0x03)
    if registers is None:
        return None

    # Temperature-only probes use the legacy SHANHENG register layout but
    # leave the humidity register at zero. Treat that as missing humidity
    # before trying the swapped WMS layout, where it can look like 0 C air.
    if len(registers) >= 2 and registers[0] == 0 and registers[1] == 0:
        return SensorReading(
            sensor_type="TEMP_AMBIGUOUS",
            vendor="SHANHENG_CANDIDATE",
            temperature_c=0.0,
            humidity_pct=None,
            co2_ppm=None,
            raw_hex=response.hex(),
            details={"confidence": "ambiguous_all_zero_payload"},
        )
    if len(registers) >= 2 and registers[1] == 0:
        temperature_c = _signed16(registers[0]) * 0.1
        if -50 < temperature_c < 100:
            return SensorReading(
                sensor_type="TEMP",
                vendor="SHANHENG",
                temperature_c=temperature_c,
                humidity_pct=None,
                co2_ppm=None,
                raw_hex=response.hex(),
            )

    candidates: list[SensorReading] = []
    for vendor in ("SHANHENG", "WMS"):
        candidate = _decode_candidate(registers, vendor, response.hex())
        if candidate:
            candidates.append(candidate)

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: _candidate_score(item.temperature_c, item.humidity_pct),
        reverse=True,
    )
    return candidates[0]


def _validated_registers(
    response: bytes,
    expected_address: int,
    expected_function: int,
    expected_count: int,
) -> list[int] | None:
    if len(response) < 5 or response[0] != expected_address:
        return None
    if not has_valid_crc(response) or response[1] & 0x80:
        return None
    registers = _parse_registers(response, expected_function)
    if registers is None or len(registers) != expected_count:
        return None
    return registers


def decode_ec092_response(response: bytes, expected_address: int) -> SensorReading | None:
    registers = _validated_registers(response, expected_address, 0x04, 9)
    if registers is None:
        return None

    speed, fault, limit, reserved_1, reserved_2, bus_v, current, power, temp = registers
    heatsink_temperature = _signed16(temp)
    if reserved_1 != 0 or reserved_2 != 0:
        return None
    if not any(registers):
        return None
    if not 0 <= speed <= 50000 or not 0 <= fault <= 0xFF:
        return None
    if limit == 0xFFFF or power > 10000:
        return None
    if not 0 <= bus_v <= 1000 or not 0 <= current <= 50000:
        return None
    if not -50 <= heatsink_temperature <= 200:
        return None

    return SensorReading(
        sensor_type="EC092",
        vendor="SUNXFAN",
        temperature_c=None,
        humidity_pct=None,
        co2_ppm=None,
        raw_hex=response.hex(),
        details={
            "speed_rpm": speed,
            "fault_code": fault,
            "limit_code": limit,
            "bus_voltage_v": bus_v,
            "phase_current_a": round(current * 0.01, 2),
            "power_w": power,
            "heatsink_temperature_c": heatsink_temperature,
        },
    )


def decode_adl_meter_response(response: bytes, expected_address: int) -> SensorReading | None:
    registers = _validated_registers(response, expected_address, 0x03, 7)
    if registers is None:
        return None

    voltage, current, active, reactive, apparent, power_factor, frequency = registers
    active_signed = _signed16(active)
    reactive_signed = _signed16(reactive)
    power_factor_signed = _signed16(power_factor)
    if not any(registers):
        return None
    if not 0 <= voltage <= 6000 or not 0 <= current <= 50000:
        return None
    if not -1000 <= power_factor_signed <= 1000 or not 0 <= frequency <= 7000:
        return None
    if apparent == 0xFFFF:
        return None

    return SensorReading(
        sensor_type="ELECTRICITY_METER",
        vendor="ADL",
        temperature_c=None,
        humidity_pct=None,
        co2_ppm=None,
        raw_hex=response.hex(),
        details={
            "voltage_v": round(voltage * 0.1, 1),
            "current_a": round(current * 0.01, 2),
            "active_power_kw": round(active_signed * 0.001, 3),
            "reactive_power_kvar": round(reactive_signed * 0.001, 3),
            "apparent_power_kva": round(apparent * 0.001, 3),
            "power_factor": round(power_factor_signed * 0.001, 3),
            "frequency_hz": round(frequency * 0.01, 2),
        },
    )


def _decode_profile_response(
    profile: str,
    response: bytes,
    expected_address: int,
) -> SensorReading | None:
    if profile == "th":
        return decode_sensor_response(response, expected_address)
    if profile == "ec092":
        return decode_ec092_response(response, expected_address)
    if profile == "meter":
        return decode_adl_meter_response(response, expected_address)
    raise ValueError(f"unknown profile: {profile}")


def parse_server_response(command_id: str, raw_response: str) -> tuple[bytes | None, str]:
    line = raw_response.strip()
    parts = line.split(":")
    if len(parts) < 2 or parts[0] != command_id:
        return None, "unexpected response"
    if parts[1] == "ERROR":
        if len(parts) < 4:
            return None, "invalid response timestamp"
        error = ":".join(parts[2:-1]) or "modbus server error"
        timestamp_text = parts[-1]
    else:
        if len(parts) != 3:
            return None, "unexpected response"
        error = ""
        timestamp_text = parts[2]

    try:
        timestamp = float(timestamp_text)
    except ValueError:
        return None, "invalid response timestamp"
    if not math.isfinite(timestamp) or timestamp < 0:
        return None, "invalid response timestamp"
    if error:
        return None, error
    try:
        return bytes.fromhex(parts[1]), ""
    except ValueError:
        return None, "response was not hex"


def _error_status(error: str) -> str:
    normalized = error.lower()
    if "modbus exception" in normalized:
        return "EXCEPTION"
    if normalized.startswith("incomplete response after") or normalized.startswith(
        "no response after clearing"
    ):
        return "MISS"
    return "ERROR"


def _modbus_exception_code(
    response: bytes,
    expected_address: int,
    expected_function: int,
) -> int | None:
    if len(response) != 5 or response[0] != expected_address:
        return None
    if response[1] != (expected_function | 0x80) or not has_valid_crc(response):
        return None
    return response[2]


def send_modbus_command_line(
    *,
    modbus_host: str,
    modbus_port: int,
    command_id: str,
    line: str,
    timeout: float,
) -> tuple[bytes | None, str]:
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout + 1.0

    try:
        with socket.create_connection((modbus_host, modbus_port), timeout=timeout + 1.0) as sock:
            sock.settimeout(timeout + 1.0)
            sock.sendall(line.encode("ascii"))
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
    except OSError as exc:
        return None, f"could not reach modbus server: {exc}"

    if not chunks:
        return None, "no response from modbus server"

    raw_response = b"".join(chunks).decode("ascii", errors="replace")
    return parse_server_response(command_id, raw_response)


def probe_address(
    *,
    port: str,
    baudrate: int,
    address: int,
    register_count: int,
    modbus_host: str,
    modbus_port: int,
    timeout: float,
) -> ScanResult:
    frame = build_read_holding_frame(address, 0x0000, register_count)
    response_length = 3 + register_count * 2 + 2
    command_id = (
        f"sensor_scan_{port.rsplit('/', 1)[-1]}_{baudrate}_"
        f"{address:02x}_{register_count}_{int(time.time() * 1000)}"
    )
    line = build_modbus_server_line(
        command_id=command_id,
        port=port,
        baudrate=baudrate,
        frame=frame,
        response_length=response_length,
        timeout=timeout,
    )
    response, error = send_modbus_command_line(
        modbus_host=modbus_host,
        modbus_port=modbus_port,
        command_id=command_id,
        line=line,
        timeout=timeout,
    )
    expected_label = EXPECTED_ADDRESS_LABELS.get(address)

    if response is None:
        return ScanResult(
            port=port,
            baudrate=baudrate,
            address=address,
            probe_registers=register_count,
            reading=None,
            status=_error_status(error),
            note=error,
            expected_label=expected_label,
            probe_function=0x03,
            start_register=0x0000,
        )

    exception_code = _modbus_exception_code(response, address, 0x03)
    if exception_code is not None:
        return ScanResult(
            port=port,
            baudrate=baudrate,
            address=address,
            probe_registers=register_count,
            reading=None,
            status="EXCEPTION",
            note=f"Modbus exception code=0x{exception_code:02X}",
            expected_label=expected_label,
            probe_function=0x03,
            start_register=0x0000,
        )

    if _validated_registers(response, address, 0x03, register_count) is None:
        return ScanResult(
            port=port,
            baudrate=baudrate,
            address=address,
            probe_registers=register_count,
            reading=None,
            status="UNKNOWN",
            note=f"response did not contain the expected {register_count} registers",
            expected_label=expected_label,
            probe_function=0x03,
            start_register=0x0000,
        )

    reading = decode_sensor_response(response, address)
    if reading is None:
        return ScanResult(
            port=port,
            baudrate=baudrate,
            address=address,
            probe_registers=register_count,
            reading=None,
            status="UNKNOWN",
            note=f"response did not decode as TH/THC: {response.hex()}",
            expected_label=expected_label,
            probe_function=0x03,
            start_register=0x0000,
        )

    return ScanResult(
        port=port,
        baudrate=baudrate,
        address=address,
        probe_registers=register_count,
        reading=reading,
        status="CANDIDATE",
        note="profile match without an identity register",
        expected_label=expected_label,
        probe_function=0x03,
        start_register=0x0000,
    )


def probe_profile_address(
    *,
    profile: str,
    port: str,
    baudrate: int,
    address: int,
    function_code: int,
    start_register: int,
    register_count: int,
    modbus_host: str,
    modbus_port: int,
    timeout: float,
) -> ScanResult:
    frame = build_read_frame(address, function_code, start_register, register_count)
    response_length = 3 + register_count * 2 + 2
    command_id = (
        f"device_scan_{profile}_{port.rsplit('/', 1)[-1]}_{baudrate}_"
        f"{address:02x}_{function_code:02x}_{start_register}_{register_count}_"
        f"{int(time.time() * 1000)}"
    )
    line = build_modbus_server_line(
        command_id=command_id,
        port=port,
        baudrate=baudrate,
        frame=frame,
        response_length=response_length,
        timeout=timeout,
    )
    response, error = send_modbus_command_line(
        modbus_host=modbus_host,
        modbus_port=modbus_port,
        command_id=command_id,
        line=line,
        timeout=timeout,
    )
    expected_label = EXPECTED_ADDRESS_LABELS.get(address)
    if response is None:
        return ScanResult(
            port=port,
            baudrate=baudrate,
            address=address,
            probe_registers=register_count,
            reading=None,
            status=_error_status(error),
            note=error,
            expected_label=expected_label,
            probe_function=function_code,
            start_register=start_register,
        )

    exception_code = _modbus_exception_code(response, address, function_code)
    if exception_code is not None:
        return ScanResult(
            port=port,
            baudrate=baudrate,
            address=address,
            probe_registers=register_count,
            reading=None,
            status="EXCEPTION",
            note=f"Modbus exception code=0x{exception_code:02X}",
            expected_label=expected_label,
            probe_function=function_code,
            start_register=start_register,
        )

    reading = _decode_profile_response(profile, response, address)
    if reading is None:
        return ScanResult(
            port=port,
            baudrate=baudrate,
            address=address,
            probe_registers=register_count,
            reading=None,
            status="UNKNOWN",
            note=(
                f"response did not decode as {profile}; function=0x{function_code:02X}; "
                f"start={start_register}; raw={response.hex()}"
            ),
            expected_label=expected_label,
            probe_function=function_code,
            start_register=start_register,
        )

    return ScanResult(
        port=port,
        baudrate=baudrate,
        address=address,
        probe_registers=register_count,
        reading=reading,
        status="CANDIDATE",
        note=(
            f"profile match without identity register; "
            f"function=0x{function_code:02X}; start={start_register}"
        ),
        expected_label=expected_label,
        probe_function=function_code,
        start_register=start_register,
    )


def scan_sensors(
    *,
    ports: list[str],
    baudrates: list[int],
    addresses: list[int],
    modbus_host: str,
    modbus_port: int,
    timeout: float,
    include_misses: bool = False,
) -> list[ScanResult]:
    results: list[ScanResult] = []
    for port in ports:
        for baudrate in baudrates:
            for address in addresses:
                address_results: list[ScanResult] = []
                for register_count in (11, 2):
                    result = probe_address(
                        port=port,
                        baudrate=baudrate,
                        address=address,
                        register_count=register_count,
                        modbus_host=modbus_host,
                        modbus_port=modbus_port,
                        timeout=timeout,
                    )
                    address_results.append(result)
                    if result.found:
                        break

                primary = next(
                    (item for item in address_results if item.status == "FOUND"),
                    None,
                )
                if primary is None:
                    primary = next(
                        (item for item in address_results if item.status == "CANDIDATE"),
                        None,
                    )
                if primary is not None:
                    results.append(primary)
                results.extend(
                    item
                    for item in address_results
                    if item.status not in {"FOUND", "CANDIDATE", "MISS"}
                )
                if include_misses:
                    results.extend(item for item in address_results if item.status == "MISS")
    return results


def scan_profile(
    *,
    profile: str,
    ports: list[str],
    baudrates: list[int],
    addresses: list[int],
    fan_register_bases: list[int],
    modbus_host: str,
    modbus_port: int,
    timeout: float,
    include_misses: bool = False,
) -> list[ScanResult]:
    if profile == "th":
        return scan_sensors(
            ports=ports,
            baudrates=baudrates,
            addresses=addresses,
            modbus_host=modbus_host,
            modbus_port=modbus_port,
            timeout=timeout,
            include_misses=include_misses,
        )

    results: list[ScanResult] = []
    specs = profile_probe_specs(profile, fan_register_bases)
    for port in ports:
        for baudrate in baudrates:
            for address in profile_addresses(profile, addresses):
                attempts: list[ScanResult] = []
                for function_code, start_register, register_count in specs:
                    result = probe_profile_address(
                        profile=profile,
                        port=port,
                        baudrate=baudrate,
                        address=address,
                        function_code=function_code,
                        start_register=start_register,
                        register_count=register_count,
                        modbus_host=modbus_host,
                        modbus_port=modbus_port,
                        timeout=timeout,
                    )
                    attempts.append(result)
                    if result.found:
                        results.append(result)
                        break
                if not any(item.found for item in attempts):
                    evidence = [item for item in attempts if item.status != "MISS"]
                    if evidence:
                        results.extend(evidence)
                    elif include_misses:
                        results.extend(attempts)
    return results


def scan_devices(
    *,
    profiles: list[str],
    ports: list[str],
    baudrates: list[int],
    addresses: list[int],
    fan_register_bases: list[int],
    modbus_host: str,
    modbus_port: int,
    timeout: float,
    include_misses: bool = False,
) -> list[ScanResult]:
    results: list[ScanResult] = []
    for profile in profiles:
        results.extend(
            scan_profile(
                profile=profile,
                ports=ports,
                baudrates=baudrates,
                addresses=addresses,
                fan_register_bases=fan_register_bases,
                modbus_host=modbus_host,
                modbus_port=modbus_port,
                timeout=timeout,
                include_misses=include_misses,
            )
        )
    return results


def format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def print_results(results: list[ScanResult], *, include_misses: bool) -> None:
    headers = [
        "status",
        "port",
        "baud",
        "address",
        "expected",
        "type",
        "vendor",
        "temp_c",
        "rh_pct",
        "co2_ppm",
        "details",
        "probe_regs",
        "function",
        "start_register",
        "note",
    ]
    print("\t".join(headers))
    for result in results:
        reading = result.reading
        row = [
            result.status,
            result.port,
            str(result.baudrate),
            f"0x{result.address:02X}",
            result.expected_label or "",
            reading.sensor_type if reading else "",
            reading.vendor if reading else "",
            format_value(reading.temperature_c if reading else None),
            format_value(reading.humidity_pct if reading else None),
            format_value(reading.co2_ppm if reading else None),
            json.dumps(reading.details, sort_keys=True) if reading and reading.details else "",
            str(result.probe_registers),
            "" if result.probe_function is None else f"0x{result.probe_function:02X}",
            "" if result.start_register is None else str(result.start_register),
            result.note,
        ]
        print("\t".join(row))

    if not results and not include_misses:
        print("No sensors found.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Modbus scanner for TH/THC, EC092, and ADL devices."
    )
    parser.add_argument("--ports", default=",".join(DEFAULT_PORTS))
    parser.add_argument(
        "--baudrates",
        default=",".join(str(value) for value in DEFAULT_BAUDRATES),
        help="Comma-separated baud rates, e.g. 9600 or 9600,38400.",
    )
    parser.add_argument(
        "--addresses",
        default=DEFAULT_ADDRESS_TEXT,
        help="Comma-separated addresses/ranges, e.g. 0x10-0x15,0x20.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Scan all slave addresses 0x01-0xF7 instead of the known-address set.",
    )
    parser.add_argument(
        "--profiles",
        default=",".join(DEFAULT_PROFILES),
        help="Comma-separated profiles: th,ec092,meter.",
    )
    parser.add_argument(
        "--fan-register-bases",
        default=",".join(str(value) for value in DEFAULT_FAN_REGISTER_BASES),
        help="EC092 PDU start registers, tried in order.",
    )
    parser.add_argument("--modbus-host", default=DEFAULT_MODBUS_HOST)
    parser.add_argument("--modbus-port", type=int, default=DEFAULT_MODBUS_PORT)
    parser.add_argument("--timeout", type=float, default=CHANNEL_READ_TIMEOUT_SECONDS)
    parser.add_argument("--include-misses", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        ports = parse_port_list(args.ports)
        baudrates = parse_int_list(args.baudrates)
        addresses = parse_int_list(args.addresses)
        profiles = parse_profile_list(args.profiles)
        fan_register_bases = parse_int_list(args.fan_register_bases)
        if args.full:
            addresses = list(DEFAULT_ADDRESSES)
        validate_scan_inputs(addresses, fan_register_bases)
        validate_baudrates(baudrates)
        validate_timeout(args.timeout)
        validate_modbus_port(args.modbus_port)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    if "meter" in profiles and not profile_addresses("meter", addresses):
        print(
            "Skipping meter profile: address filter excludes decimal 147 and 194.",
            file=sys.stderr,
        )

    results = scan_devices(
        profiles=profiles,
        ports=ports,
        baudrates=baudrates,
        addresses=addresses,
        fan_register_bases=fan_register_bases,
        modbus_host=args.modbus_host,
        modbus_port=args.modbus_port,
        timeout=args.timeout,
        include_misses=args.include_misses,
    )

    if args.as_json:
        print(json.dumps([result.as_dict() for result in results], indent=2, sort_keys=True))
    else:
        print_results(results, include_misses=args.include_misses)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
