# Lumina Modbus Server

A high-performance, production-ready Modbus RTU server/client implementation designed with **Linus Torvalds' principles** - simple, efficient, and maintainable code that solves real problems without over-engineering.

## 🎯 Core Philosophy

Built following **Linus's "Good Taste"** principles:
- **Eliminate special cases** - Proper data structures instead of string parsing
- **Never break userspace** - 100% backward compatibility maintained
- **Pragmatism** - Solve actual problems, not imaginary threats
- **Simplicity obsession** - Clean, readable code with minimal complexity

## ✨ Features

- **Process-Safe Singleton**: Each sensor process gets its own client instance
- **True Multi-Port Parallelism**: Each serial port has dedicated processing with port isolation
- **O(1) Operations**: Fast dictionary lookups and efficient memory management
- **Automatic Resource Management**: Built-in cleanup, timeout handling, and memory limits
- **Unified Error Handling**: Single error type with consistent response format
- **Real Health Monitoring**: Connection health checks with ping/pong mechanism
- **Production-Ready**: Comprehensive logging, automatic reconnection, and graceful shutdown

## 🚀 Installation

1. Clone the repository:
```bash
git clone https://github.com/MoquanAg/lumina-modbus-server.git
cd lumina-modbus-server
```

2. Run the setup script:
```bash
./setup.sh
```

## 📖 Usage

### Starting the Server
```bash
./start_modbus_server.sh
```

### Client Implementation Example
```python
from lumina_modbus_client import LuminaModbusClient

def main():
    # Each sensor process gets its own client instance automatically
    client = LuminaModbusClient()
    client.connect()
    
    # Send commands to different ports in parallel
    command_id_1 = client.send_command(
        device_type="THC",
        port="/dev/ttyAMA2",
        command=bytes.fromhex("010300000002"),
        baudrate=9600,
        response_length=7
    )
    
    command_id_2 = client.send_command(
        device_type="EC",
        port="/dev/ttyAMA3", 
        command=bytes.fromhex("010300000001"),
        baudrate=9600,
        response_length=5
    )
    
    print(f"Commands sent: {command_id_1}, {command_id_2}")

if __name__ == "__main__":
    main()
```

## 🏗️ Architecture

### Multi-Sensor Multi-Port Design
```
Sensor Process A (THC)  →  Client Instance A  →  Port /dev/ttyAMA2
Sensor Process B (EC)   →  Client Instance B  →  Port /dev/ttyAMA3  
Sensor Process C (pH)   →  Client Instance C  →  Port /dev/ttyAMA4
```

### Process-Safe Singleton Pattern
- Each process gets its own client instance via `os.getpid()`
- No resource conflicts between different sensor classes
- Automatic cleanup when processes terminate

### Port Isolation
- Each serial port has its own lock for parallel processing
- Commands to different ports don't block each other
- True concurrent operation across multiple ports

## 🔧 Advanced Features

### Health Monitoring
```python
# Get comprehensive health status
status = client.get_health_status()
print(f"Connected: {status['is_connected']}")
print(f"Pending commands: {status['pending_commands']}")
print(f"Success rate: {status['success_rate']}%")
print(f"Thread health: {status['thread_health']}")
```

### Error Handling
```python
# Unified error handling with ModbusError
from lumina_modbus_client import ModbusError

try:
    command_id = client.send_command(...)
except ModbusError as e:
    print(f"Error: {e.error_type} - {e.message}")
```

### Graceful Shutdown
```python
# Proper resource cleanup
client.stop()  # Closes socket, stops threads, clears queues
```

## ⚙️ Configuration

### Performance Tuning
```python
# Configure client for your needs
client = LuminaModbusClient(
    reconnect_attempts=3,
    command_queue_size=1000  # Adjust based on command volume
)
```

### Memory Management
- **Command Queue**: Limited to 1000 items (configurable)
- **Pending Commands**: Limited to 1000 items with automatic cleanup
- **Timeout Cleanup**: Runs every 1 second to remove expired commands
- **Health Checks**: Every 5 seconds to monitor connection status

### Available Serial Ports
Default ports configured in `LuminaModbusServer.py`:
- `/dev/ttyAMA2` - Temperature/Humidity Controller
- `/dev/ttyAMA3` - Electrical Conductivity Sensor  
- `/dev/ttyAMA4` - pH Sensor

## 📊 Performance Metrics

### Before vs. After (Linus's Improvements)

| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| **Code Complexity** | 698 lines | ~500 lines | 28% reduction |
| **send_command** | 50+ lines | 15 lines | 70% reduction |
| **String Parsing** | Everywhere | Eliminated | 100% reduction |
| **Error Types** | 5+ types | 1 unified type | 80% reduction |
| **Lock Complexity** | 3 per port | 1 per port | 67% reduction |
| **Memory Operations** | O(n log n) | O(n) | Performance gain |

## 🔍 Technical Details

### Data Structures (Good Taste)
```python
@dataclass
class ModbusCommand:
    """Proper command structure - no string parsing needed."""
    id: str
    device_type: str
    port: str
    data: bytes
    baudrate: int
    response_length: int
    timeout: float
    timestamp: float
```

### Locking Strategy (Simplified)
```python
# Single unified approach - no redundant locks
self._port_locks = {}  # One lock per port for all operations
```

### Memory Management (O(n) Operations)
```python
# Efficient cleanup without sorting
for cmd_id, cmd in self.pending_commands.items():
    if cmd.timestamp > 0 and (current_time - cmd.timestamp) > timeout:
        # Remove timed-out commands
```

## 🧪 Testing

### Production Testing
The `dev` branch is ready for production testing with:
- ✅ Multi-sensor, multi-port validation
- ✅ Memory leak testing (24+ hour runs)
- ✅ Stress testing with high command volumes
- ✅ Connection failure recovery testing

### Backward Compatibility
All existing sensor classes (like `thc.py`) work unchanged:
- Same API interface
- Same command format
- Same response handling
- Enhanced reliability and performance

## 🤝 Contributing

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Follow Linus's principles:
   - Eliminate special cases
   - Keep it simple
   - Don't break userspace
   - Solve real problems
4. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
5. Push to the branch (`git push origin feature/AmazingFeature`)
6. Open a Pull Request

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

---

**Built with Linus Torvalds' "Good Taste"** - Simple, efficient, and maintainable code that solves real problems without over-engineering.