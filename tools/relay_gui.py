#!/usr/bin/env python3
"""Local relay-board test GUI for lumina-modbus-server.

This tool serves a small localhost web UI and sends Modbus RTU frames through
the existing lumina-modbus-server TCP protocol. It does not talk to serial
hardware directly.
"""

from __future__ import annotations

import argparse
import configparser
import html
import json
import re
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


CHANNEL_COUNT = 16
RESPONSE_LENGTH = 8

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8766
DEFAULT_MODBUS_HOST = "127.0.0.1"
DEFAULT_MODBUS_PORT = 8888
DEFAULT_SERIAL_PORT = "/dev/ttyAMA1"
DEFAULT_BAUDRATE = 38400
DEFAULT_SLAVE_ADDRESS = 0x01
DEFAULT_COMMAND_TIMEOUT = 2.5
DEFAULT_PULSE_SECONDS = 10.0
MAX_DURATION_SECONDS = 600.0

ACTION_LOCK = threading.Lock()


class RelayConfig:
    """Runtime settings for the local relay test UI."""

    def __init__(
        self,
        *,
        serial_port: str = DEFAULT_SERIAL_PORT,
        baudrate: int = DEFAULT_BAUDRATE,
        slave_address: int = DEFAULT_SLAVE_ADDRESS,
        modbus_host: str = DEFAULT_MODBUS_HOST,
        modbus_port: int = DEFAULT_MODBUS_PORT,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        pulse_seconds: float = DEFAULT_PULSE_SECONDS,
        channel_labels: dict[int, str] | None = None,
        exclusive_groups: list[list[int]] | None = None,
    ) -> None:
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.slave_address = slave_address
        self.modbus_host = modbus_host
        self.modbus_port = modbus_port
        self.command_timeout = command_timeout
        self.pulse_seconds = pulse_seconds
        self.channel_labels = channel_labels or {}
        self.exclusive_groups = exclusive_groups or []

    def as_dict(self) -> dict[str, Any]:
        return {
            "serial_port": self.serial_port,
            "baudrate": self.baudrate,
            "slave_address": self.slave_address,
            "slave_address_hex": f"0x{self.slave_address:02X}",
            "modbus_host": self.modbus_host,
            "modbus_port": self.modbus_port,
            "command_timeout": self.command_timeout,
            "pulse_seconds": self.pulse_seconds,
            "channel_count": CHANNEL_COUNT,
            "channel_labels": self.channel_labels,
            "exclusive_groups": self.exclusive_groups,
        }

    def exclusive_peers_for(self, channel: int) -> list[int]:
        peers: list[int] = []
        for group in self.exclusive_groups:
            if channel in group:
                peers.extend(peer for peer in group if peer != channel)
        return sorted(set(peers))


def parse_slave_address(value: str) -> int:
    try:
        address = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("address must be an integer or hex value") from exc

    if not 0 <= address <= 0xFF:
        raise argparse.ArgumentTypeError("address must be between 0x00 and 0xFF")
    return address


def _require_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def humanize_device_name(name: str) -> str:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name.strip())
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    spaced = re.sub(r"\bFCU\b", "FCU", spaced, flags=re.IGNORECASE)
    spaced = spaced.replace("On Off", "on/off")
    words = spaced.split()
    if not words:
        return name

    normalized = []
    for word in words:
        if word.upper() == "FCU":
            normalized.append("FCU")
        elif word == "on/off":
            normalized.append(word)
        else:
            normalized.append(word.lower())
    normalized[0] = normalized[0].capitalize()
    return " ".join(normalized)


def _parse_relay_control_value(value: str) -> dict[str, Any]:
    parts = [part.strip().strip('"') for part in value.split(",")]
    if len(parts) < 6:
        return {}

    return {
        "serial_port": parts[3],
        "slave_address": int(parts[4], 0),
        "baudrate": int(parts[5]),
    }


def _parse_assignment_range(key: str, relay_board: str) -> tuple[int, int] | None:
    match = re.fullmatch(rf"relay_{re.escape(relay_board.lower())}_(\d+)_to_(\d+)", key.lower())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _parse_exclusive_group(value: str) -> list[int]:
    channels: list[int] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        match = re.search(r"(\d+)$", token)
        if not match:
            continue
        channel = int(match.group(1))
        validate_channel(channel)
        if channel not in channels:
            channels.append(channel)
    return channels


def load_device_conf_settings(path: str | Path, relay_board: str = "one") -> dict[str, Any]:
    config_path = Path(path).expanduser()
    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"), strict=False)
    read_files = parser.read(config_path)
    if not read_files:
        raise ValueError(f"could not read device config: {config_path}")

    settings: dict[str, Any] = {
        "channel_labels": {},
        "exclusive_groups": [],
    }
    relay_board = relay_board.lower()

    if parser.has_section("RELAY_CONTROL"):
        relay_key = f"relay{relay_board}"
        if parser.has_option("RELAY_CONTROL", relay_key):
            settings.update(_parse_relay_control_value(parser.get("RELAY_CONTROL", relay_key)))

    if parser.has_section("RELAY_ASSIGNMENTS"):
        for key, value in parser.items("RELAY_ASSIGNMENTS"):
            relay_range = _parse_assignment_range(key, relay_board)
            if relay_range is None:
                continue

            start, end = relay_range
            names = [item.strip() for item in value.split(",")]
            for offset, name in enumerate(names):
                channel = start + offset
                if channel > end or channel >= CHANNEL_COUNT:
                    break
                if name.lower() in ("", "null", "none"):
                    continue
                settings["channel_labels"][channel] = humanize_device_name(name)

    if parser.has_section("RELAY_EXCLUSIVE_GROUPS"):
        for _, value in parser.items("RELAY_EXCLUSIVE_GROUPS"):
            group = _parse_exclusive_group(value)
            if len(group) > 1:
                settings["exclusive_groups"].append(group)

    return settings


def modbus_crc(payload: bytes) -> bytes:
    """Return the Modbus RTU CRC bytes in low-byte, high-byte order."""
    crc = 0xFFFF
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def build_write_single_coil_frame(slave_address: int, channel: int, state: bool) -> bytes:
    """Build function-05 Modbus RTU frame for a zero-based relay channel."""
    slave_address = _require_int("slave_address", slave_address)
    channel = _require_int("channel", channel)

    if not 0 <= slave_address <= 0xFF:
        raise ValueError("slave_address must be between 0x00 and 0xFF")
    if not 0 <= channel < CHANNEL_COUNT:
        raise ValueError(f"channel must be between 0 and {CHANNEL_COUNT - 1}")

    value_high = 0xFF if state else 0x00
    body = bytes((slave_address, 0x05, 0x00, channel, value_high, 0x00))
    return body + modbus_crc(body)


def build_modbus_server_line(
    *,
    command_id: str,
    port: str,
    baudrate: int,
    frame: bytes,
    timeout: float,
) -> str:
    """Build one lumina-modbus-server command line."""
    return f"{command_id}:relay:{port}:{baudrate}:{frame.hex()}:{RESPONSE_LENGTH}:{timeout}\n"


def parse_modbus_server_response(command_id: str, raw_response: str) -> dict[str, Any]:
    line = raw_response.strip()
    parts = line.split(":", 3)

    if len(parts) < 2 or parts[0] != command_id:
        return {
            "ok": False,
            "command_id": command_id,
            "raw": line,
            "error": "unexpected response from modbus server",
        }

    if parts[1] == "ERROR":
        return {
            "ok": False,
            "command_id": command_id,
            "raw": line,
            "error": parts[2] if len(parts) >= 3 else "modbus server error",
        }

    return {
        "ok": True,
        "command_id": command_id,
        "raw": line,
        "response_hex": parts[1],
        "timestamp": parts[2] if len(parts) >= 3 else None,
    }


def send_modbus_command_line(config: RelayConfig, line: str, command_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + config.command_timeout
    chunks: list[bytes] = []

    try:
        with socket.create_connection(
            (config.modbus_host, config.modbus_port),
            timeout=config.command_timeout,
        ) as sock:
            sock.settimeout(config.command_timeout)
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
        return {
            "ok": False,
            "command_id": command_id,
            "error": f"could not reach modbus server: {exc}",
        }

    if not chunks:
        return {
            "ok": False,
            "command_id": command_id,
            "error": "modbus server returned no response",
        }

    raw_response = b"".join(chunks).decode("ascii", errors="replace")
    return parse_modbus_server_response(command_id, raw_response)


def set_relay_channel(config: RelayConfig, channel: int, state: bool) -> dict[str, Any]:
    frame = build_write_single_coil_frame(config.slave_address, channel, state)
    command_id = (
        f"relaygui_ch{channel}_{'on' if state else 'off'}_{int(time.time() * 1000)}"
    )
    line = build_modbus_server_line(
        command_id=command_id,
        port=config.serial_port,
        baudrate=config.baudrate,
        frame=frame,
        timeout=config.command_timeout,
    )
    result = send_modbus_command_line(config, line, command_id)
    result.update(
        {
            "channel": channel,
            "state": state,
            "request_hex": frame.hex(),
        }
    )
    return result


def set_relay_channel_with_interlocks(
    config: RelayConfig,
    channel: int,
    state: bool,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    affected_channels: dict[int, bool] = {}

    if state:
        for peer in config.exclusive_peers_for(channel):
            peer_result = set_relay_channel(config, peer, False)
            results.append(peer_result)
            affected_channels[peer] = False
            if not peer_result["ok"]:
                return {
                    "ok": False,
                    "channel": channel,
                    "state": state,
                    "results": results,
                    "affected_channels": affected_channels,
                    "error": peer_result.get("error"),
                }

    target_result = set_relay_channel(config, channel, state)
    results.append(target_result)
    affected_channels[channel] = state

    return {
        "ok": target_result["ok"],
        "channel": channel,
        "state": state,
        "results": results,
        "affected_channels": affected_channels,
        "error": None if target_result["ok"] else target_result.get("error"),
    }


def validate_channel(value: Any) -> int:
    channel = _require_int("channel", value)
    if not 0 <= channel < CHANNEL_COUNT:
        raise ValueError(f"channel must be between 0 and {CHANNEL_COUNT - 1}")
    return channel


def validate_duration(value: Any, default: float) -> float:
    if value is None:
        duration = default
    else:
        try:
            duration = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("duration must be a number") from exc

    if not 0 < duration <= MAX_DURATION_SECONDS:
        raise ValueError(f"duration must be between 0 and {MAX_DURATION_SECONDS:g} seconds")
    return duration


def pulse_channel(config: RelayConfig, channel: int, duration: float) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    on_result = set_relay_channel_with_interlocks(config, channel, True)
    results.append(on_result)

    try:
        if not on_result["ok"]:
            return {"ok": False, "channel": channel, "results": results, "error": on_result["error"]}
        time.sleep(duration)
        off_result = set_relay_channel(config, channel, False)
        results.append(off_result)
        return {
            "ok": off_result["ok"],
            "channel": channel,
            "duration": duration,
            "results": results,
            "error": None if off_result["ok"] else off_result.get("error"),
        }
    finally:
        if results[-1].get("state") is not False:
            results.append(set_relay_channel(config, channel, False))


def turn_all_off(config: RelayConfig, channels: range = range(CHANNEL_COUNT)) -> dict[str, Any]:
    results = [set_relay_channel(config, channel, False) for channel in channels]
    failed = [result for result in results if not result["ok"]]
    return {
        "ok": not failed,
        "results": results,
        "error": None if not failed else f"{len(failed)} relay command(s) failed",
    }


def run_zero_to_five_sequence(config: RelayConfig, duration: float) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    ok = True
    error = None

    try:
        for channel in range(6):
            on_result = set_relay_channel(config, channel, True)
            results.append(on_result)
            if not on_result["ok"]:
                ok = False
                error = on_result.get("error")
                break

            time.sleep(duration)

            off_result = set_relay_channel(config, channel, False)
            results.append(off_result)
            if not off_result["ok"]:
                ok = False
                error = off_result.get("error")
                break
    finally:
        cleanup = turn_all_off(config, range(6))
        results.extend(cleanup["results"])
        if ok and not cleanup["ok"]:
            ok = False
            error = cleanup.get("error")

    return {
        "ok": ok,
        "duration": duration,
        "results": results,
        "error": error,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Relay Bench Control</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --panel-strong: #eef2f5;
      --text: #172026;
      --muted: #59656f;
      --line: #d7dee4;
      --primary: #116b8c;
      --primary-hover: #0d5c78;
      --on: #197647;
      --off: #9d2c2e;
      --pulse: #8a5a00;
      --disabled: #a3adb7;
      --shadow: 0 10px 30px rgba(23, 32, 38, 0.08);
    }

    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #111518;
        --panel: #1a2025;
        --panel-strong: #222a30;
        --text: #edf2f5;
        --muted: #aab5be;
        --line: #33404a;
        --primary: #58b8d9;
        --primary-hover: #79c9e3;
        --on: #47b87a;
        --off: #ef7779;
        --pulse: #dda947;
        --disabled: #58636d;
        --shadow: 0 10px 30px rgba(0, 0, 0, 0.25);
      }
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    main {
      width: min(1180px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 28px 0 36px;
    }

    header {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 20px;
    }

    h1 {
      margin: 0;
      font-size: 28px;
      line-height: 1.15;
      letter-spacing: 0;
    }

    .settings {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
      max-width: 680px;
    }

    .setting {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 8px 10px;
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }

    .setting strong {
      color: var(--text);
      font-weight: 650;
    }

    .bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 18px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
    }

    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .status {
      min-height: 24px;
      color: var(--muted);
      font-size: 14px;
      text-align: right;
    }

    button {
      appearance: none;
      border: 1px solid transparent;
      border-radius: 8px;
      min-height: 38px;
      padding: 0 14px;
      font: inherit;
      font-weight: 650;
      color: #ffffff;
      cursor: pointer;
      transition: transform 120ms ease, border-color 120ms ease, background 120ms ease;
    }

    button:hover:not(:disabled) {
      transform: translateY(-1px);
    }

    button:focus-visible {
      outline: 3px solid rgba(88, 184, 217, 0.35);
      outline-offset: 2px;
    }

    button:disabled {
      cursor: wait;
      background: var(--disabled);
      color: rgba(255, 255, 255, 0.8);
    }

    .btn-primary {
      background: var(--primary);
    }

    .btn-primary:hover:not(:disabled) {
      background: var(--primary-hover);
    }

    .btn-on {
      background: var(--on);
    }

    .btn-off {
      background: var(--off);
    }

    .btn-pulse {
      background: var(--pulse);
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }

    .channel {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 12px;
    }

    .channel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 12px;
    }

    .channel-title {
      font-weight: 750;
      font-size: 16px;
    }

    .channel-label {
      margin-top: 4px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }

    .channel-state {
      border-radius: 999px;
      background: var(--panel-strong);
      color: var(--muted);
      min-width: 74px;
      padding: 5px 8px;
      text-align: center;
      font-size: 12px;
      font-weight: 700;
    }

    .channel-actions {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }

    .channel-actions button {
      width: 100%;
      min-width: 0;
      padding: 0 8px;
      font-size: 13px;
    }

    .log {
      margin-top: 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      min-height: 120px;
      max-height: 260px;
      overflow: auto;
      padding: 12px;
      font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      color: var(--muted);
      white-space: pre-wrap;
    }

    @media (max-width: 880px) {
      header {
        display: block;
      }

      .settings {
        justify-content: flex-start;
        margin-top: 14px;
      }

      .bar {
        display: block;
      }

      .status {
        margin-top: 10px;
        text-align: left;
      }

      .grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }

    @media (max-width: 520px) {
      main {
        width: min(100vw - 20px, 460px);
        padding-top: 18px;
      }

      h1 {
        font-size: 23px;
      }

      .grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Relay Bench Control</h1>
      <div class="settings" aria-label="Relay settings">
        <span class="setting">Port <strong>__SERIAL_PORT__</strong></span>
        <span class="setting">Baud <strong>__BAUDRATE__</strong></span>
        <span class="setting">Address <strong>__SLAVE_ADDRESS_HEX__</strong></span>
        <span class="setting">Modbus server <strong>__MODBUS_HOST__:__MODBUS_PORT__</strong></span>
      </div>
    </header>

    <section class="bar" aria-label="Global actions">
      <div class="actions">
        <button class="btn-off" id="allOffButton" type="button">All off</button>
        <button class="btn-primary" id="sequenceButton" type="button">Run 0-5 sequence</button>
      </div>
      <div class="status" id="status">Idle</div>
    </section>

    <section class="grid" id="channelGrid" aria-label="Relay channels"></section>
    <section class="log" id="log" aria-label="Command log"></section>
  </main>

  <script>
    const config = {
      channelCount: __CHANNEL_COUNT__,
      pulseSeconds: __PULSE_SECONDS__,
      channelLabels: __CHANNEL_LABELS__,
      exclusiveGroups: __EXCLUSIVE_GROUPS__
    };
    const grid = document.getElementById("channelGrid");
    const logEl = document.getElementById("log");
    const statusEl = document.getElementById("status");
    const buttons = [];
    const states = new Map();

    function now() {
      return new Date().toLocaleTimeString();
    }

    function log(message) {
      logEl.textContent = `[${now()}] ${message}\n` + logEl.textContent;
    }

    function setBusy(isBusy, message) {
      buttons.forEach((button) => {
        button.disabled = isBusy;
      });
      statusEl.textContent = message || (isBusy ? "Working..." : "Idle");
    }

    function setChannelState(channel, text) {
      states.set(channel, text);
      const node = document.querySelector(`[data-channel-state="${channel}"]`);
      if (node) {
        node.textContent = text;
      }
    }

    async function postJSON(path, body, busyText) {
      setBusy(true, busyText);
      try {
        const response = await fetch(path, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body || {})
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) {
          throw new Error(payload.error || `HTTP ${response.status}`);
        }
        return payload;
      } finally {
        setBusy(false);
      }
    }

    async function setRelay(channel, state) {
      const label = state ? "On" : "Off";
      log(`Channel ${channel}: ${label}`);
      try {
        const payload = await postJSON("/api/relay", {channel, state}, `Setting channel ${channel} ${label.toLowerCase()}...`);
        applyAffectedChannels(payload);
        log(`Channel ${channel}: OK`);
      } catch (error) {
        log(`Channel ${channel}: ${error.message}`);
        statusEl.textContent = error.message;
      }
    }

    function applyAffectedChannels(payload) {
      if (!payload.affected_channels) {
        return;
      }

      Object.entries(payload.affected_channels).forEach(([channel, state]) => {
        setChannelState(Number(channel), state ? "On" : "Off");
      });
    }

    async function pulseRelay(channel) {
      log(`Channel ${channel}: pulse ${config.pulseSeconds}s`);
      try {
        await postJSON(
          "/api/pulse",
          {channel, duration: config.pulseSeconds},
          `Pulsing channel ${channel} for ${config.pulseSeconds}s...`
        );
        setChannelState(channel, "Off");
        log(`Channel ${channel}: pulse complete`);
      } catch (error) {
        log(`Channel ${channel}: ${error.message}`);
        statusEl.textContent = error.message;
      }
    }

    async function allOff() {
      log("All off");
      try {
        await postJSON("/api/all-off", {}, "Turning all channels off...");
        for (let channel = 0; channel < config.channelCount; channel += 1) {
          setChannelState(channel, "Off");
        }
        log("All channels: Off");
      } catch (error) {
        log(`All off: ${error.message}`);
        statusEl.textContent = error.message;
      }
    }

    async function runSequence() {
      const confirmed = window.confirm("Run channels 0 through 5, 10 seconds each?");
      if (!confirmed) {
        return;
      }

      log("Sequence 0-5: start");
      try {
        await postJSON(
          "/api/sequence-0-5",
          {duration: config.pulseSeconds},
          `Running sequence 0-5 at ${config.pulseSeconds}s each...`
        );
        for (let channel = 0; channel <= 5; channel += 1) {
          setChannelState(channel, "Off");
        }
        log("Sequence 0-5: complete");
      } catch (error) {
        log(`Sequence 0-5: ${error.message}`);
        statusEl.textContent = error.message;
      }
    }

    function render() {
      for (let channel = 0; channel < config.channelCount; channel += 1) {
        const card = document.createElement("article");
        card.className = "channel";

        const head = document.createElement("div");
        head.className = "channel-head";

        const title = document.createElement("div");
        title.className = "channel-title";
        title.textContent = `Channel ${channel}`;

        const label = document.createElement("div");
        label.className = "channel-label";
        label.textContent = config.channelLabels[channel] || "Unassigned";

        const state = document.createElement("div");
        state.className = "channel-state";
        state.dataset.channelState = channel;
        state.textContent = "Unknown";

        const titleBlock = document.createElement("div");
        titleBlock.append(title, label);

        head.append(titleBlock, state);

        const actions = document.createElement("div");
        actions.className = "channel-actions";

        const onButton = document.createElement("button");
        onButton.className = "btn-on";
        onButton.type = "button";
        onButton.textContent = "On";
        onButton.addEventListener("click", () => setRelay(channel, true));

        const offButton = document.createElement("button");
        offButton.className = "btn-off";
        offButton.type = "button";
        offButton.textContent = "Off";
        offButton.addEventListener("click", () => setRelay(channel, false));

        const pulseButton = document.createElement("button");
        pulseButton.className = "btn-pulse";
        pulseButton.type = "button";
        pulseButton.textContent = "Pulse";
        pulseButton.title = `Turn on for ${config.pulseSeconds}s, then off`;
        pulseButton.addEventListener("click", () => pulseRelay(channel));

        buttons.push(onButton, offButton, pulseButton);
        actions.append(onButton, offButton, pulseButton);
        card.append(head, actions);
        grid.append(card);
      }

      const allOffButton = document.getElementById("allOffButton");
      const sequenceButton = document.getElementById("sequenceButton");
      buttons.push(allOffButton, sequenceButton);
      allOffButton.addEventListener("click", allOff);
      sequenceButton.addEventListener("click", runSequence);
      log("Ready");
    }

    render();
  </script>
</body>
</html>
"""


def render_html(config: RelayConfig) -> str:
    channel_labels_json = json.dumps(config.channel_labels).replace("</", "<\\/")
    exclusive_groups_json = json.dumps(config.exclusive_groups).replace("</", "<\\/")
    return (
        HTML_TEMPLATE.replace("__SERIAL_PORT__", html.escape(config.serial_port))
        .replace("__BAUDRATE__", str(config.baudrate))
        .replace("__SLAVE_ADDRESS_HEX__", f"0x{config.slave_address:02X}")
        .replace("__MODBUS_HOST__", html.escape(config.modbus_host))
        .replace("__MODBUS_PORT__", str(config.modbus_port))
        .replace("__CHANNEL_COUNT__", str(CHANNEL_COUNT))
        .replace("__PULSE_SECONDS__", f"{config.pulse_seconds:g}")
        .replace("__CHANNEL_LABELS__", channel_labels_json)
        .replace("__EXCLUSIVE_GROUPS__", exclusive_groups_json)
    )


class RelayRequestHandler(BaseHTTPRequestHandler):
    relay_config: RelayConfig
    server_version = "RelayGui/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.send_html(render_html(self.relay_config))
            return

        if path == "/api/config":
            self.send_json(HTTPStatus.OK, {"ok": True, "config": self.relay_config.as_dict()})
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self.read_json()
            if path == "/api/relay":
                result = self.handle_relay(payload)
            elif path == "/api/pulse":
                result = self.handle_pulse(payload)
            elif path == "/api/all-off":
                result = self.handle_all_off()
            elif path == "/api/sequence-0-5":
                result = self.handle_sequence(payload)
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
                return
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return

        status = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_GATEWAY
        self.send_json(status, result)

    def handle_relay(self, payload: dict[str, Any]) -> dict[str, Any]:
        channel = validate_channel(payload.get("channel"))
        state = payload.get("state")
        if not isinstance(state, bool):
            raise ValueError("state must be true or false")

        with ACTION_LOCK:
            return set_relay_channel_with_interlocks(self.relay_config, channel, state)

    def handle_pulse(self, payload: dict[str, Any]) -> dict[str, Any]:
        channel = validate_channel(payload.get("channel"))
        duration = validate_duration(payload.get("duration"), self.relay_config.pulse_seconds)

        with ACTION_LOCK:
            return pulse_channel(self.relay_config, channel, duration)

    def handle_all_off(self) -> dict[str, Any]:
        with ACTION_LOCK:
            return turn_all_off(self.relay_config)

    def handle_sequence(self, payload: dict[str, Any]) -> dict[str, Any]:
        duration = validate_duration(payload.get("duration"), self.relay_config.pulse_seconds)

        with ACTION_LOCK:
            return run_zero_to_five_sequence(self.relay_config, duration)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}

        raw = self.rfile.read(length).decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("request body must be valid JSON") from exc

        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def make_handler(config: RelayConfig) -> type[RelayRequestHandler]:
    class ConfiguredRelayRequestHandler(RelayRequestHandler):
        relay_config = config

    return ConfiguredRelayRequestHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve a local relay test GUI.")
    parser.add_argument("--http-host", default=DEFAULT_HTTP_HOST)
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument("--modbus-host", default=DEFAULT_MODBUS_HOST)
    parser.add_argument("--modbus-port", type=int, default=DEFAULT_MODBUS_PORT)
    parser.add_argument("--serial-port")
    parser.add_argument("--baudrate", type=int)
    parser.add_argument("--address", type=parse_slave_address)
    parser.add_argument("--timeout", type=float, default=DEFAULT_COMMAND_TIMEOUT)
    parser.add_argument("--pulse-seconds", type=float, default=DEFAULT_PULSE_SECONDS)
    parser.add_argument("--device-conf")
    parser.add_argument("--relay-board", default="one")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    device_conf_settings: dict[str, Any] = {}
    if args.device_conf:
        device_conf_settings = load_device_conf_settings(args.device_conf, args.relay_board)

    config = RelayConfig(
        serial_port=args.serial_port
        or device_conf_settings.get("serial_port", DEFAULT_SERIAL_PORT),
        baudrate=args.baudrate
        if args.baudrate is not None
        else device_conf_settings.get("baudrate", DEFAULT_BAUDRATE),
        slave_address=args.address
        if args.address is not None
        else device_conf_settings.get("slave_address", DEFAULT_SLAVE_ADDRESS),
        modbus_host=args.modbus_host,
        modbus_port=args.modbus_port,
        command_timeout=args.timeout,
        pulse_seconds=validate_duration(args.pulse_seconds, DEFAULT_PULSE_SECONDS),
        channel_labels=device_conf_settings.get("channel_labels"),
        exclusive_groups=device_conf_settings.get("exclusive_groups"),
    )

    server = ThreadingHTTPServer((args.http_host, args.http_port), make_handler(config))
    server.daemon_threads = True
    display_host = "127.0.0.1" if args.http_host in ("", "0.0.0.0") else args.http_host

    print(f"Relay GUI: http://{display_host}:{args.http_port}")
    print(
        "Relay target: "
        f"{config.serial_port} @ {config.baudrate}, address 0x{config.slave_address:02X}, "
        f"modbus server {config.modbus_host}:{config.modbus_port}"
    )
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping relay GUI.")
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
