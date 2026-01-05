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

## Architecture

```
TCP Clients → LuminaModbusServer → Command Queues (per port) → PyModbus → Serial Ports
```

- **main.py**: TCP server with per-port thread pools; maintains connection pool (`pymodbus_connections`) keyed by `[port][baudrate]`
- **examples/client/**: Production client templates to copy into other projects—singleton client with async event-driven and synchronous APIs, plus high-level Modbus functions (`read_holding_registers`, `write_register`, etc.)
- **LuminaLogger.py**: Rotating log handler (5MB/file, 20MB total)

Protocol: `command_id:device_type:port:baudrate:hex:length[:timeout]\n` → `command_id:hex:timestamp\n`

## Key Design Decisions

- Server runs separately from client processes—never import `main.py` into other projects, communicate via TCP only
- PyModbus handles timing, CRC, and retries automatically
- Each serial port has its own asyncio event loop in a dedicated thread
- Available ports hardcoded in `AVAILABLE_PORTS` constant

## Performance Considerations

Multiple sensors across different ports are polled frequently. The system is tuned so a slow or unresponsive sensor won't block readings on other ports:

- **Low retries**: PyModbus configured with `retries=1` to fail fast rather than blocking the queue
- **Minimal command spacing**: 50ms minimum gap between commands on same port (`min_command_spacing`)
- **Per-port isolation**: Each port has its own thread and command queue—a hung sensor on one port cannot block sensors on other ports
- **Short timeouts**: Default connection timeout is 1 second; command-specific timeouts passed through from clients

Keep these constraints in mind when adding sensors or modifying timing.
