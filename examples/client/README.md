# Lumina Modbus Client Examples

These are template/example files for future projects that need to communicate with the Lumina Modbus Server.

## Files

### lumina_modbus_client.py
A complete, production-ready Modbus client with:
- **Singleton pattern** for global client management
- **Async event-driven architecture** with pub/sub pattern via `ModbusEventEmitter`
- **Synchronous API** with blocking calls for simple use cases
- **High-level Modbus functions**:
  - `read_holding_registers()` - Modbus function code 0x03
  - `read_coils()` - Modbus function code 0x01
  - `write_register()` - Modbus function code 0x06
  - `write_registers()` - Modbus function code 0x10
- **Low-level command API** via `send_command()` for custom protocols
- **Automatic reconnection** with exponential backoff
- **Per-port locking** to support multiple serial ports concurrently
- **Health monitoring** and command queue management

### lumina_modbus_event_emitter.py
Event emitter for async handling of Modbus responses:
- Subscribe callbacks to specific device types
- Thread-safe queue-based response distribution
- Built-in monitoring and health checks

## Usage Examples

### Basic Connection and Read

```python
from lumina_modbus_client import LuminaModbusClient

# Initialize and connect
client = LuminaModbusClient()
client.connect(host='127.0.0.1', port=8888)

# Read holding registers (synchronous)
response = client.read_holding_registers(
    port='/dev/ttyAMA3',
    address=0x0000,
    count=2,
    slave_addr=0x01,
    baudrate=9600,
    timeout=1.0
)

if not response.isError():
    print(f"Register values: {response.registers}")
else:
    print(f"Error: {response.error}")
```

### Write Operations

```python
# Write single register
result = client.write_register(
    port='/dev/ttyAMA3',
    address=0x0000,
    value=0x1234,
    slave_addr=0x01,
    baudrate=9600
)

# Write multiple registers
result = client.write_registers(
    port='/dev/ttyAMA3',
    address=0x0000,
    values=[0x1234, 0x5678, 0x9ABC],
    slave_addr=0x01,
    baudrate=9600
)

if not result.isError():
    print("Write successful")
```

### Async Event-Driven Pattern

```python
def handle_sensor_response(response):
    """Callback for sensor data"""
    if response.status == 'success':
        print(f"Sensor data: {response.data.hex()}")
    else:
        print(f"Error: {response.status}")

# Subscribe to device type
client.event_emitter.subscribe('THC_SENSOR', handle_sensor_response)

# Send command (non-blocking)
command_id = client.send_command(
    device_type='THC_SENSOR',
    port='/dev/ttyAMA2',
    command=bytearray([0x01, 0x03, 0x00, 0x00, 0x00, 0x02]),
    baudrate=9600,
    response_length=9,
    timeout=1.0
)
# Callback will be invoked when response arrives
```

### Custom Low-Level Commands

```python
# For custom protocols or non-standard Modbus
command_id = client.send_command(
    device_type='CUSTOM_DEVICE',
    port='/dev/ttyAMA4',
    command=bytearray([0x01, 0x06, 0x00, 0x01, 0x00, 0x03]),
    baudrate=4800,
    response_length=8,
    timeout=2.0
)
```

## Architecture Notes

- **Thread-safe**: Multiple threads can safely send commands concurrently
- **Per-port isolation**: Commands to different ports are fully parallelized
- **Automatic CRC**: CRC16 is automatically calculated and appended
- **Reconnection**: Automatic reconnection on connection loss
- **Timeout handling**: Per-command timeout configuration
- **Queue management**: Built-in queue overflow protection and monitoring

## Integration

To use in your project:

1. Copy both files to your project directory
2. Install dependencies: `pyserial` (no other dependencies needed)
3. Ensure the Lumina Modbus Server is running on the target host/port
4. Import and initialize:

```python
from lumina_modbus_client import LuminaModbusClient
client = LuminaModbusClient()
client.connect()
```

## Server Requirements

These clients expect a Lumina Modbus Server listening on a TCP socket (default: `127.0.0.1:8888`).

Protocol format:
```
<command_id>:<device_type>:<port>:<baudrate>:<command_hex>:<response_length>[:<timeout>]\n
```

Response format:
```
<command_id>:<response_hex>:<timestamp>\n
OR
<command_id>:ERROR:<error_type>:<timestamp>\n
```

## Last Updated

Synced from: `/home/lumina/lumina-edge/communication/modbus/`  
Date: 2025-11-24

