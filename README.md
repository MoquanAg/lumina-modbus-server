# Lumina Modbus Server

**A standalone TCP service that provides multi-process access to Modbus RTU serial devices.**

This repository contains:
1. **LuminaModbusServer** - The PyModbus-based TCP server (runs as a service)
2. **Client Templates** - Production-ready client code in `examples/client/` (copy to your projects)

## What It Does

Solves the "port already open" problem when multiple processes need to talk to serial Modbus devices:

```text
┌─────────────────────────┐
│  lumina-modbus-server   │  Standalone Service
│  (this repository)      │  - Manages serial ports
│                         │  - Uses PyModbus internally
│  Port 8888 (TCP)        │  - Runs as systemd service
└─────────┬───────────────┘
          │ TCP
    ┌─────┴─────┬─────────┬─────────┐
    │           │         │         │
┌───▼───┐  ┌───▼───┐ ┌───▼───┐ ┌───▼────┐
│main.py│  │sensor │ │debug  │ │ telnet │
│       │  │manager│ │scripts│ │        │
└───────┘  └───────┘ └───────┘ └────────┘
  lumina-edge processes + debug tools
```

**Before:** Only one process can open `/dev/ttyAMA1` at a time  
**After:** All processes connect to `127.0.0.1:8888` simultaneously

## Features

### Server Features
✅ **Multi-process access** - Multiple processes connect to one serial port  
✅ **PyModbus powered** - Robust timing, CRC, retries handled automatically  
✅ **Simple protocol** - Text-based TCP commands (telnet-friendly)  
✅ **Auto-reconnection** - Handles serial port recovery  
✅ **Production ready** - Systemd service with logging  
✅ **Dynamic connection pooling** - Per-port/baudrate connection management

### Client Templates (examples/client/)
✅ **Production-ready client** - Copy and use in your projects  
✅ **Dual API** - Both async event-driven and synchronous blocking  
✅ **High-level Modbus** - PyModbus-style API (read/write registers)  
✅ **Low-level commands** - Full control for custom protocols  
✅ **Well-documented** - Comprehensive README with examples  
✅ **Battle-tested** - Synced from production lumina-edge code

## Quick Start

### 1. Install Dependencies

```bash
cd ~/lumina-modbus-server
pip install -r requirements.txt
```

### 2. Test Locally

```bash
# Start server
python main.py

# Test with telnet (in another terminal)
telnet 127.0.0.1 8888
test:THC:/dev/ttyAMA1:9600:010300000002C40B:9:0.5
```

> **Note:** For production clients, see the [Client Integration](#client-integration) section below.

### 3. Install as System Service (Recommended)

```bash
cd ~/lumina-modbus-server

# Create systemd service
sudo tee /etc/systemd/system/lumina-modbus-server.service > /dev/null << 'SERVICE'
[Unit]
Description=Lumina Modbus Server (PyModbus)
After=network.target

[Service]
Type=simple
User=lumina
WorkingDirectory=/home/lumina/lumina-modbus-server
ExecStart=/usr/bin/python main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable lumina-modbus-server
sudo systemctl start lumina-modbus-server
sudo systemctl status lumina-modbus-server
```

## Client Integration

### For Existing lumina-edge Projects

**No code changes needed!** Your existing sensors already use LuminaModbusClient:

```python
# In lumina-edge/communication/modbus/client.py - Works as-is!
from communication.modbus.client import LuminaModbusClient

client = LuminaModbusClient()
client.connect(host="127.0.0.1", port=8888)

# Send command exactly as before
command_id = client.send_command(
    device_type="THC",
    port="/dev/ttyAMA1",
    command=bytes.fromhex("010300000002C40B"),
    baudrate=9600,
    response_length=9,
    timeout=0.5
)

# Response comes via event emitter (same as always)
```

### For New Projects

Copy the client templates from `examples/client/` to your project:

```bash
# Copy client files to your project
cp examples/client/lumina_modbus_client.py your_project/
cp examples/client/lumina_modbus_event_emitter.py your_project/
```

See `examples/client/README.md` for complete documentation and usage examples.

#### Quick Example

```python
from lumina_modbus_client import LuminaModbusClient

# Initialize and connect
client = LuminaModbusClient()
client.connect(host='127.0.0.1', port=8888)

# High-level Modbus API (synchronous)
response = client.read_holding_registers(
    port='/dev/ttyAMA3',
    address=0x0000,
    count=2,
    slave_addr=0x01,
    baudrate=9600
)

if not response.isError():
    print(f"Register values: {response.registers}")
```

#### Features of the Client Templates

✅ **Async event-driven** - Subscribe to device responses  
✅ **Synchronous API** - Blocking calls for simple use cases  
✅ **High-level Modbus** - `read_holding_registers()`, `write_register()`, etc.  
✅ **Low-level commands** - Full control with `send_command()`  
✅ **Auto-reconnect** - Handles connection failures  
✅ **Thread-safe** - Multiple concurrent commands  
✅ **Per-port isolation** - Parallel operations on different ports

See `examples/client/README.md` for detailed documentation.

## TCP Protocol

Simple text-based format (telnet-friendly for debugging):

### Request Format

```text
command_id:device_type:port:baudrate:command_hex:response_length[:timeout]\n
```

**Fields:**
- `command_id` - Unique identifier for tracking responses
- `device_type` - Device type label (e.g., 'THC', 'EC', 'MODBUS')
- `port` - Serial port path (e.g., '/dev/ttyAMA1')
- `baudrate` - Serial baudrate (e.g., 9600, 4800)
- `command_hex` - Command bytes in hex format (CRC included)
- `response_length` - Expected response length in bytes
- `timeout` - Optional timeout in seconds (default: 5.0)

**Example:**
```text
test123:THC:/dev/ttyAMA1:9600:010300000002C40B:9:0.5
```

### Response Format

```text
command_id:response_hex:timestamp\n
```

**Success Example:**
```text
test123:0103040064000012AB:1732467890.123
```

**Error Example:**
```text
test123:ERROR:timeout:1732467890.456
```

**Error Types:**
- `timeout` - Command timed out waiting for response
- `serial_error` - Serial port communication failure
- `invalid_port` - Port not available or doesn't exist
- `connection_failed` - PyModbus connection failure

### Manual Testing

```bash
# Test with telnet
telnet 127.0.0.1 8888
test:THC:/dev/ttyAMA1:9600:010300000002C40B:9:0.5
# Press Enter and wait for response
```

## Architecture

### What Changed (PyModbus Upgrade)

**Before:**
```python
# Custom serial code
ser = serial.Serial(port, baudrate, ...)
ser.write(command)
response = ser.read(expected_length)
# Manual timing, CRC, error handling
```

**After:**
```python
# PyModbus handles everything
client = AsyncModbusSerialClient(port, baudrate, ...)
await client.connect()
response = await client.read_holding_registers(addr, count, slave=addr)
# Timing, CRC, retries automatic
```

### Why TCP Service?

The server runs **separately** from lumina-edge because:
- Serial ports can only be opened by one process
- Multiple lumina-edge processes need sensor access
- Debug scripts need to run while system is live
- Systemd manages reliability (auto-restart, logging)

**Important:** Never copy `main.py` into lumina-edge. They communicate via TCP, not shared code.

## Management

### Service Control

```bash
# Start/stop/restart
sudo systemctl start lumina-modbus-server
sudo systemctl stop lumina-modbus-server
sudo systemctl restart lumina-modbus-server

# View status
sudo systemctl status lumina-modbus-server

# View logs
sudo journalctl -u lumina-modbus-server -f

# Enable/disable autostart
sudo systemctl enable lumina-modbus-server
sudo systemctl disable lumina-modbus-server
```

### Logs

```bash
# Real-time logs
sudo journalctl -u lumina-modbus-server -f

# Last 100 lines
sudo journalctl -u lumina-modbus-server -n 100

# Today's logs
sudo journalctl -u lumina-modbus-server --since today
```

## Troubleshooting

### Server won't start

```bash
# Check if port 8888 is in use
sudo netstat -tlnp | grep 8888

# Check serial port permissions
ls -l /dev/ttyAMA*
sudo usermod -a -G dialout lumina  # Add user to dialout group
```

### Client can't connect

```bash
# Verify server is running
sudo systemctl status lumina-modbus-server

# Test connection
telnet 127.0.0.1 8888

# Check firewall (should allow loopback by default)
sudo iptables -L | grep 8888
```

### Serial port errors

```bash
# Check if physical port exists
ls -l /dev/ttyAMA1

# Check journalctl for detailed errors
sudo journalctl -u lumina-modbus-server -n 50
```

## Testing

### Manual Testing with Telnet

```bash
# Connect to server
telnet 127.0.0.1 8888

# Send a test command
test:THC:/dev/ttyAMA1:9600:010300000002C40B:9:0.5

# Expected response format:
# command_id:response_hex:timestamp
# OR
# command_id:ERROR:error_type:timestamp
```

### Testing with Client Library

See `examples/client/README.md` for complete client usage examples.

## Configuration

### Serial Ports

Available ports are configured in `main.py`:

```python
# Default available ports (Raspberry Pi)
AVAILABLE_PORTS = [
    '/dev/ttyAMA0',
    '/dev/ttyAMA1', 
    '/dev/ttyAMA2',
    '/dev/ttyAMA3',
    '/dev/ttyAMA4'
]
```

The server dynamically manages connections per port/baudrate combination.

### TCP Server

```python
# Default settings in __main__
HOST = "127.0.0.1"  # Localhost only (secure)
PORT = 8888         # TCP port
MAX_QUEUE = 100     # Command queue size per port
TIMEOUT = 30        # Request timeout (seconds)
```

To change settings, edit the bottom of `main.py`:

```python
if __name__ == '__main__':
    server = LuminaModbusServer(
        host='0.0.0.0',      # Listen on all interfaces
        port=8888,
        max_queue_size=200,
        request_timeout=60
    )
    server.start()
```

## Requirements

- Python 3.8+
- PyModbus 3.6.0+
- asyncio (built-in)
- pyserial

See `requirements.txt` for complete list.

## Benefits Over Direct Serial Access

| Feature | Direct Serial | TCP Server |
|---------|---------------|------------|
| Multi-process | ❌ Port conflicts | ✅ Unlimited clients |
| Debug while running | ❌ Must stop system | ✅ Debug anytime |
| Error handling | ⚠️ Manual | ✅ PyModbus robust |
| Timing accuracy | ⚠️ Custom code | ✅ PyModbus standard |
| Maintenance | ❌ Your problem | ✅ PyModbus team |

## Contributing

1. Fork the repository
2. Create feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit changes (`git commit -m 'Add AmazingFeature'`)
4. Push to branch (`git push origin feature/AmazingFeature`)
5. Open Pull Request

## License

MIT License - see LICENSE file for details.

## Repository Structure

```
lumina-modbus-server/
├── main.py                     # Main server (PyModbus-based)
├── LuminaLogger.py             # Server logging utilities
├── requirements.txt            # Python dependencies
├── setup.sh                    # Installation script
├── start_modbus_server.sh      # Server startup script
└── examples/
    └── client/
        ├── README.md                      # Comprehensive client docs
        ├── lumina_modbus_client.py        # Client template (copy to your project)
        └── lumina_modbus_event_emitter.py # Event emitter template
```

---

**Questions?** 
- Server usage: See this README
- Client integration: See `examples/client/README.md`
- Protocol details: See [TCP Protocol](#tcp-protocol) section above
