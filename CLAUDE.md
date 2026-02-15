# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TCP gateway providing multi-process access to Modbus RTU serial devices. Solves the "port already open" problem—multiple clients connect to `127.0.0.1:8888` instead of directly opening serial ports.

## Commands

```bash
# Run server
python main.py

# Run tests with coverage
pytest --cov=. --cov-report=term tests/

# Run single test
pytest tests/test_lumina_logger.py::TestLuminaLogger::test_logger_initialization -v

# Manual test via telnet
telnet 127.0.0.1 8888
# Format: command_id:device_type:port:baudrate:command_hex:response_length:timeout
```

## Process Management

- **ONLY use `start_modbus_server.sh`** to start the server — it launches an lxterminal GUI window. Do NOT use `systemctl`, `setsid`, `nohup`, or any background/headless process method.
- The start script handles: venv activation, killing existing processes, and launching in a visible terminal.

## Architecture

```
TCP Clients → LuminaModbusServer → Command Queues (per port) → Raw Pyserial → Serial Ports
```

- **main.py**: TCP server with per-port threads; maintains serial connection pool (`serial_connections`) keyed by `[port][baudrate]`
- **examples/client/**: Production client templates—singleton client with async event-driven and synchronous APIs, plus high-level Modbus functions (`read_holding_registers`, `write_register`, etc.)
- **LuminaLogger.py**: Rotating log handler (5MB/file, 20MB total)

Protocol: `command_id:device_type:port:baudrate:hex:length:timeout\n` → `command_id:hex:timestamp\n`

## Key Design Decisions

- **Raw pyserial instead of PyModbus**: PyModbus async operations couldn't be cancelled reliably, causing hangs on unresponsive sensors. Raw pyserial provides native timeout control.
- **No asyncio**: Purely synchronous I/O—simpler and more reliable timeouts
- **Manual CRC validation**: Server validates CRC on responses, drains buffer on mismatch
- **Fail fast**: No retries in server—CRC errors return immediately, client decides retry policy
- **Port watchdog**: Detects frozen per-port serial threads (120s inactivity threshold). Recovers by closing serial connections, forcing the blocked `read()` to raise `SerialException`. Runs every 30s.
- **Write response validation**: Modbus write functions (0x05, 0x06, 0x10) return responses identical to command bytes. CRC validation distinguishes actual write responses from TX echoes.
- **Crash diagnostics**: Raw file I/O crash logger (bypasses LuminaLogger) captures full thread dumps and server state on unhandled exceptions. Log rotation protected with thread-safe locking.
- Server runs separately from client processes—communicate via TCP only
- Available ports hardcoded in `AVAILABLE_PORTS` constant

## Performance Considerations

Multiple sensors across different ports are polled frequently. The system is tuned so a slow or unresponsive sensor won't block readings on other ports:

- **Dynamic command spacing**: Baud-rate-aware gaps between commands (150ms for 9600 baud, 50ms for 115200+)
- **Stale command skip**: Commands older than their timeout are skipped to prevent queue backlog
- **Per-port isolation**: Each port has its own thread and command queue—a hung sensor on one port cannot block sensors on other ports
- **Buffer drain on errors**: After CRC mismatch, stale bytes are drained to prevent contaminating next read
- **Client-specific timeouts**: Passed through from client, pyserial uses native timeout

## Known Hardware Considerations

RS-485 signal integrity affects reliability. Common issues seen:
- CRC errors from electrical noise or reflections
- `FF` bytes from missing bias resistors
- Echo patterns from missing termination resistors

Software handles these gracefully (fail fast, drain buffers), but hardware fixes improve success rate.
