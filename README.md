# Lumina Modbus Server

**A standalone TCP service that provides multi-process access to Modbus RTU serial devices.**

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

✅ **Multi-process access** - Run main.py, sensor_manager.py, and debug scripts simultaneously  
✅ **PyModbus powered** - Robust timing, CRC, retries handled automatically  
✅ **Zero client changes** - Existing lumina-edge sensors work as-is  
✅ **Simple protocol** - Text-based TCP commands (telnet-friendly)  
✅ **Auto-reconnection** - Handles serial port recovery  
✅ **Production ready** - Systemd service with logging

## Quick Start

### 1. Install Dependencies

```bash
cd ~/lumina-modbus-server
pip install -r requirements.txt
```

### 2. Test Locally

```bash
# Terminal 1: Start server
python3 LuminaModbusServer.py

# Terminal 2: Test with your sensors
cd ~/lumina-edge
python3 sensors/thc.py
```

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
ExecStart=/usr/bin/python3 LuminaModbusServer.py
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

## Client Usage

**No code changes needed in lumina-edge!** Your existing sensors already use LuminaModbusClient:

```python
# In lumina-edge/sensors/thc.py - Works as-is!
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

## TCP Protocol

Simple text-based format (for debugging or custom clients):

```text
REQUEST:
command_id:device_type:port:baudrate:command_hex:response_length:timeout

EXAMPLE:
test:THC:/dev/ttyAMA1:9600:010300000002C40B:9:0.5

RESPONSE:
OK:command_id:response_hex
ERROR:command_id:error_message
```

### Manual Testing

```bash
# Test with telnet
telnet 127.0.0.1 8888
test:THC:/dev/ttyAMA1:9600:010300000002C40B:9:0.5
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

**Important:** Never copy `LuminaModbusServer.py` into lumina-edge. They communicate via TCP, not shared code.

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

Run the test suite:

```bash
cd ~/lumina-modbus-server
python3 test_pymodbus_server.py
```

Expected output:
```
✓ PASS: Server Import
✓ PASS: Server Startup
✓ PASS: Client Connection
🎉 All tests passed!
```

## Configuration

### Serial Ports

Configure in `LuminaModbusServer.py`:

```python
# Default ports
self.available_ports = {
    "/dev/ttyAMA1": None,
    "/dev/ttyAMA2": None,
    "/dev/ttyAMA3": None,
    "/dev/ttyAMA4": None,
}
```

### TCP Server

```python
# Default settings
HOST = "127.0.0.1"  # Localhost only (secure)
PORT = 8888         # TCP port
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

---

**Questions?** Check the test files for usage examples:
- `test_pymodbus_server.py` - Server test suite
- `thc.py` - Real sensor example
