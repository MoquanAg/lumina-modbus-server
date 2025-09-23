# Lumina Modbus Server

A high-performance, asynchronous Modbus RTU server/client implementation with multi-port support and robust error handling.

## Features

- **Asynchronous Architecture**: Built with `asyncio` for optimal performance
- **Multi-Port Support**: Handles multiple serial ports simultaneously
- **Automatic Reconnection**: Built-in connection recovery
- **Comprehensive Logging**: Detailed logging with rotation support
- **Command Queuing**: Efficient command processing with timeout handling
- **CRC16 Verification**: Built-in error checking
- **Advanced Utilities**: Rich set of helper classes for enhanced functionality

## Installation

1. Clone the repository:
```bash
git clone https://github.com/lumina-ai/lumina-modbus-server.git
```
2. Run the setup script:
```bash
./setup.sh
```
## Usage

### Starting the Server
```bash
./start_modbus_server.sh
```


### Client Implementation Example
```python
import asyncio
from lumina_modbus_client import LuminaModbusClient
async def main():
client = LuminaModbusClient()
await client.connect()
# Send a command
command_id = await client.send_command(
name="read_holding",
port="/dev/ttyAMA2",
command=bytes.fromhex("010300000002"),
baudrate=9600,
response_length=7
)
asyncio.run(main())
```


## Advanced Features

### Retry Management
Handle failed commands with automatic retries and exponential backoff:
```python
from Helpers import RetryManager
retry_manager = RetryManager(max_retries=3, backoff_factor=1.5)
result = await retry_manager.execute_with_retry(client.send_command, command_data)
```
### Command Prioritization
Implement priority-based command queuing:
python
from Helpers import CommandPrioritizer
prioritizer = CommandPrioritizer()
await prioritizer.enqueue_command("high", command_data)
next_command = await prioritizer.get_next_command()
```

### Performance Monitoring
Track command execution performance:
```python
from Helpers import PerformanceMonitor
monitor = PerformanceMonitor()
monitor.record_command_execution(port="/dev/ttyAMA2", command_id="cmd1", execution_time=0.5)
report = monitor.get_port_performance_report()
```

### Command Validation
Validate commands before execution:
```python
from Helpers import CommandValidator
validator = CommandValidator()
is_valid = await validator.validate_command(command_data)
```

### Connection Pool Management
Manage multiple connections efficiently:
```python
from Helpers import ConnectionPool
pool = ConnectionPool(max_connections=10)
connection = await pool.get_connection(port="/dev/ttyAMA2", baudrate=9600)
```

### Device State Management
Track and manage device states:
```python
from Helpers import DeviceStateManager
state_manager = DeviceStateManager()
await state_manager.update_state("device1", new_state_data)
```

### Queue Analytics
Monitor command queue performance:
```python
from Helpers import QueueAnalytics
analytics = QueueAnalytics()
analytics.record_enqueue("main_queue", "cmd1")
stats = analytics.get_queue_statistics("main_queue")
```

### Response Parsing
Parse command responses with predefined patterns:
```python
from Helpers import ResponseParser
parser = ResponseParser()
parser.register_response_pattern("read_holding", pattern_definition)
parsed_data = parser.parse_response("read_holding", response_bytes)
```


### Device Configuration Management
Manage device configurations:
```python
from Helpers import DeviceConfigManager
config_manager = DeviceConfigManager()
config_manager.register_device_config("device_type1", config_schema)
await config_manager.set_device_config("device1", "device_type1", config_data)
```

### Logging Implementation
```python
from LuminaLogger import LuminaLogger
logger = LuminaLogger("MyApplication")
logger.info("Application started")
logger.debug("Debug information")
logger.error("Error occurred")
```

## Configuration

### Available Serial Ports
Default ports are configured in `LuminaModbusServer.py`:
- /dev/ttyAMA2
- /dev/ttyAMA3
- /dev/ttyAMA4

### Logging
- Default log directory: `logs/`
- Maximum log file size: 5MB
- Automatic daily rotation

## Technical Details

### Protocol Timing
The server implements proper Modbus RTU timing calculations:
- Character time = 11 bits / baud rate
- Response timeout = 1.5 * character time * expected length + 20ms

### CRC Calculation
Uses standard Modbus CRC16 algorithm with configurable byte order.

## Contributing

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.