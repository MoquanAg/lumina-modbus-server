# Lumina Modbus Server

A high-performance, asynchronous Modbus RTU server/client implementation with multi-port support and robust error handling.

## Features

- **Asynchronous Architecture**: Built with `asyncio` for optimal performance
- **Multi-Port Support**: Handles multiple serial ports simultaneously
- **Automatic Reconnection**: Built-in connection recovery
- **Comprehensive Logging**: Detailed logging with rotation support
- **Command Queuing**: Efficient command processing with timeout handling
- **CRC16 Verification**: Built-in error checking

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
### Logging Implementation
python
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