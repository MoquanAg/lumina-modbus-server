#!/usr/bin/env python3
"""Scan RS-485 ports for Lumina THC/TH Modbus sensors.

The scanner talks to the local lumina-modbus-server TCP protocol. It sends only
Modbus function 0x03 reads and never writes to the bus.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import time
from typing import Any


CHANNEL_READ_TIMEOUT_SECONDS = 0.7
DEFAULT_MODBUS_HOST = "127.0.0.1"
DEFAULT_MODBUS_PORT = 8888
DEFAULT_PORTS = ("/dev/ttyAMA1", "/dev/ttyAMA2", "/dev/ttyAMA3")
DEFAULT_BAUDRATES = (9600,)
DEFAULT_ADDRESSES = tuple(range(0x10, 0x16))

EXPECTED_ADDRESS_LABELS = {
    0x10: "Supply end lower",
    0x11: "Supply end upper",
    0x12: "Return end upper",
    0x13: "Return end lower",
    0x14: "Middle third layer",
    0x15: "Outdoor",
}


class SensorReading:
    def __init__(
        self,
        *,
        sensor_type: str,
        vendor: str,
        temperature_c: float,
        humidity_pct: float,
        co2_ppm: float | None,
        raw_hex: str,
    ) -> None:
        self.sensor_type = sensor_type
        self.vendor = vendor
        self.temperature_c = temperature_c
        self.humidity_pct = humidity_pct
        self.co2_ppm = co2_ppm
        self.raw_hex = raw_hex

    def as_dict(self) -> dict[str, Any]:
        return {
            "sensor_type": self.sensor_type,
            "vendor": self.vendor,
            "temperature_c": round(self.temperature_c, 2),
            "humidity_pct": round(self.humidity_pct, 2),
            "co2_ppm": None if self.co2_ppm is None else round(self.co2_ppm, 1),
            "raw_hex": self.raw_hex,
        }


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
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.address = address
        self.probe_registers = probe_registers
        self.reading = reading
        self.status = status
        self.note = note
        self.expected_label = expected_label

    @property
    def found(self) -> bool:
        return self.reading is not None

    def as_dict(self) -> dict[str, Any]:
        base = {
            "status": self.status,
            "port": self.port,
            "baudrate": self.baudrate,
            "address": self.address,
            "address_hex": f"0x{self.address:02X}",
            "expected_label": self.expected_label,
            "probe_registers": self.probe_registers,
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
            items.extend(range(start, end + 1))
        else:
            items.append(int(chunk, 0))

    deduped: list[int] = []
    for item in items:
        if item not in deduped:
            deduped.append(item)
    return deduped


def parse_port_list(value: str) -> list[str]:
    ports = [item.strip() for item in value.split(",") if item.strip()]
    if not ports:
        raise ValueError("at least one port is required")
    return ports


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


def build_read_holding_frame(slave_address: int, start_register: int, count: int) -> bytes:
    if not 0 <= slave_address <= 0xF7:
        raise ValueError("slave_address must be between 0x00 and 0xF7")
    if not 0 <= start_register <= 0xFFFF:
        raise ValueError("start_register must fit in uint16")
    if not 1 <= count <= 125:
        raise ValueError("count must be between 1 and 125")

    body = struct.pack(">BBHH", slave_address, 0x03, start_register, count)
    return body + modbus_crc(body)


def build_modbus_server_line(
    *,
    command_id: str,
    port: str,
    baudrate: int,
    frame: bytes,
    response_length: int,
    timeout: float,
) -> str:
    return (
        f"{command_id}:sensor_scan:{port}:{baudrate}:"
        f"{frame.hex()}:{response_length}:{timeout}\n"
    )


def _parse_registers(response: bytes) -> list[int] | None:
    if len(response) < 7:
        return None
    if response[1] != 0x03:
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

    registers = _parse_registers(response)
    if registers is None:
        return None

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


def parse_server_response(command_id: str, raw_response: str) -> tuple[bytes | None, str]:
    line = raw_response.strip()
    parts = line.split(":", 3)
    if len(parts) < 2 or parts[0] != command_id:
        return None, "unexpected response"
    if parts[1] == "ERROR":
        return None, parts[2] if len(parts) >= 3 else "modbus server error"
    try:
        return bytes.fromhex(parts[1]), ""
    except ValueError:
        return None, "response was not hex"


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
            status="MISS",
            note=error,
            expected_label=expected_label,
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
        )

    return ScanResult(
        port=port,
        baudrate=baudrate,
        address=address,
        probe_registers=register_count,
        reading=reading,
        status="FOUND",
        expected_label=expected_label,
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
                        results.append(result)
                        break

                if include_misses and not any(result.found for result in address_results):
                    results.extend(address_results)
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
        "probe_regs",
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
            str(result.probe_registers),
            result.note,
        ]
        print("\t".join(row))

    if not results and not include_misses:
        print("No sensors found.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan Modbus RS-485 ports for TH/THC sensors.")
    parser.add_argument("--ports", default=",".join(DEFAULT_PORTS))
    parser.add_argument(
        "--baudrates",
        default=",".join(str(value) for value in DEFAULT_BAUDRATES),
        help="Comma-separated baud rates, e.g. 9600 or 9600,38400.",
    )
    parser.add_argument(
        "--addresses",
        default="0x10-0x15",
        help="Comma-separated addresses/ranges, e.g. 0x10-0x15,0x20.",
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
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    results = scan_sensors(
        ports=ports,
        baudrates=baudrates,
        addresses=addresses,
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
