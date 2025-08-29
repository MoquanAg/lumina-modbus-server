import logging
import time
import socket
import threading
import queue
from typing import Dict, List, Optional
import random
import string
from dataclasses import dataclass
import weakref
import select
import os

# Configuration constants
class Config:
    # Timing constants
    COMMAND_INTERVAL = 0.001  # Minimum time between commands (seconds)
    CLEANUP_INTERVAL = 1.0    # How often to cleanup timed-out commands (seconds)
    HEALTH_CHECK_INTERVAL = 5.0  # How often to check connection health (seconds)
    SOCKET_TIMEOUT = 5.0      # Socket timeout (seconds)
    READ_TIMEOUT = 0.1        # Timeout for socket reads (seconds)
    
    # Queue and memory limits
    MAX_PENDING_COMMANDS = 1000  # Maximum number of pending commands
    MAX_REQUEST_TIMES = 100      # Maximum number of request times to track
    
    # Retry settings
    MAX_RECONNECT_ATTEMPTS = 3
    RECONNECT_BASE_DELAY = 5    # Base delay for reconnection (seconds)
    RECONNECT_MAX_DELAY = 15    # Maximum delay for reconnection (seconds)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from lumina_modbus_event_emitter import ModbusEventEmitter, ModbusResponse

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

@dataclass
class ModbusResponse:
    command_id: str
    data: Optional[bytes]
    device_type: str
    status: str
    timestamp: float = 0.0

class ModbusError(Exception):
    def __init__(self, error_type: str, message: str, command_id: str = None):
        self.error_type = error_type
        self.message = message
        self.command_id = command_id
        super().__init__(f"{error_type}: {message}")

class LuminaModbusClient:
    _instances = {}  # Per-process instances
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        process_id = os.getpid()  # Get current process ID
        with cls._lock:
            if process_id not in cls._instances:
                cls._instances[process_id] = super().__new__(cls)
                # Initialize immediately to prevent race condition
                cls._instances[process_id]._initialized = False
                cls._instances[process_id]._init_lock = threading.Lock()
            return cls._instances[process_id]

    def __init__(self, reconnect_attempts: int = 3, command_queue_size: int = 1000):
        # Thread-safe initialization to prevent race conditions
        with getattr(self, '_init_lock', threading.Lock()):
            if getattr(self, '_initialized', False):
                return
            
        # Basic initialization
        self.socket = None
        self.is_connected = False
        self.event_emitter = ModbusEventEmitter()
        
        # Threading components (simplified: only 2 threads)
        self._running = True
        self.command_queue = queue.Queue(maxsize=command_queue_size)
        self.pending_commands: Dict[str, PendingCommand] = {}
        self._socket_lock = threading.Lock()
        self._port_locks = {}  # Single lock per port (simplified)
        
        # Connection details
        self._host = None
        self._port = None
        self._reconnect_attempts = reconnect_attempts
        self._last_command_time = 0
        self._command_interval = Config.COMMAND_INTERVAL
        
        # Start worker threads (simplified: only 2 threads)
        self._threads = {
            'command': threading.Thread(target=self._process_commands, name="CommandProcessor", daemon=True),
            'read': threading.Thread(target=self._read_responses, name="ResponseReader", daemon=True)
        }
        
        for thread in self._threads.values():
            thread.start()
        
        self._initialized = True
        self._start_time = time.time()
        logger.info("LuminaModbusClient initialized")
        
        # Removed request_times tracking (simplified)
        
        # Health monitoring
        self.stats = {
            'commands_sent': 0,
            'commands_failed': 0,
            'responses_received': 0,
            'timeouts': 0,
            'reconnections': 0,
            'last_command_time': 0,
            'last_response_time': 0,
            'errors': 0
        }
        
        # Thread health monitoring
        self.thread_health = {
            'command_thread_alive': True,
            'read_thread_alive': True,
            'last_health_check': time.time()
        }

    def connect(self, host='127.0.0.1', port=8888):
        """Connect to the Modbus server."""
        self._host = host
        self._port = port
        return self._establish_connection()

    def _establish_connection(self) -> bool:
        """Internal method to establish the socket connection."""
        old_socket = None
        try:
            with self._socket_lock:
                # Store old socket for proper cleanup
                if self.socket:
                    old_socket = self.socket
                    self.socket = None
                
                # Create new socket
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                
                # Set TCP keepalive parameters
                try:
                    self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                    self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
                    self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
                except AttributeError:
                    pass
                
                self.socket.connect((self._host, self._port))
                self.socket.settimeout(Config.SOCKET_TIMEOUT)
                self.is_connected = True
                logger.debug(f"Socket connected and timeout set to 5.0 seconds")
                logger.info(f"Connected to server at {self._host}:{self._port}")
                return True
                
        except socket.timeout:
            logger.error(f"Connection timeout to {self._host}:{self._port}")
            self.is_connected = False
            return False
        except socket.gaierror as e:
            logger.error(f"DNS resolution failed for {self._host}:{self._port}: {str(e)}")
            self.is_connected = False
            return False
        except ConnectionRefusedError:
            logger.error(f"Connection refused by server at {self._host}:{self._port}")
            self.is_connected = False
            return False
        except Exception as e:
            logger.error(f"Unexpected error connecting to {self._host}:{self._port}: {str(e)}")
            self.is_connected = False
            return False
        finally:
            # Always close old socket to prevent leaks
            if old_socket:
                try:
                    old_socket.close()
                    logger.debug("Closed old socket")
                except Exception as e:
                    logger.warning(f"Error closing old socket: {str(e)}")

    def send_command(self, device_type: str, port: str, command: bytes, **kwargs) -> str:
        """Queue a command to be sent to the server."""
        # Basic validation only
        if not command:
            raise ValueError("Command cannot be empty")
        
        # Create command object (no string parsing)
        cmd = ModbusCommand(
            id=self._generate_command_id(device_type, port, command),
            device_type=device_type,
            port=port,
            data=command,
            baudrate=kwargs.get('baudrate', 9600),
            response_length=kwargs.get('response_length', 0),
            timeout=kwargs.get('timeout', 5.0),
            timestamp=time.time()
        )
        
        # Queue the command object
        try:
            self.command_queue.put(cmd, timeout=1.0)
            self.pending_commands[cmd.id] = cmd
            return cmd.id
        except queue.Full:
            self._handle_error(cmd.id, device_type, ModbusError('queue_full', 'Command queue full'))
            return cmd.id

    def _generate_command_id(self, device_type: str, port: str, command: bytes) -> str:
        """Generate unique command ID."""
        truncated_hex = command.hex()[:12]
        random_suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=2))
        port_name = port.split("/")[-1]
        send_time = time.strftime('%Y%m%d%H%M%S')
        return f"{port_name}_{device_type}_{truncated_hex}_{send_time}_{random_suffix}"

    @staticmethod
    def calculate_crc16(data: bytearray, high_byte_first: bool = True) -> bytearray:
        """
        Calculate CRC16 checksum for Modbus messages.

        Args:
            data (bytearray): Data to calculate CRC for
            high_byte_first (bool): If True, returns high byte first

        Returns:
            bytearray: Calculated CRC bytes
        """
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 1:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1

        # Splitting the CRC into high and low bytes
        high_byte = crc & 0xFF
        low_byte = (crc >> 8) & 0xFF

        # Returning the CRC in the specified byte order
        if high_byte_first:
            return bytearray([high_byte, low_byte])
        else:
            return bytearray([low_byte, high_byte])

    def _check_socket_health(self) -> bool:
        """Check if socket is healthy and connected."""
        try:
            if not self.socket or not self.is_connected:
                return False
            
            # Try to get socket info to check if it's still valid
            self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            return True
        except:
            return False

    def _get_port_lock(self, port: str):
        """Get or create lock for a specific port."""
        if port not in self._port_locks:
            self._port_locks[port] = threading.Lock()
        return self._port_locks[port]

    def _process_commands(self) -> None:
        """Process commands from the queue and send them to the server."""
        while self._running:
            try:
                cmd = self.command_queue.get(timeout=0.1)
                port_lock = self._get_port_lock(cmd.port)
                
                # Check socket health before sending
                if not self._check_socket_health():
                    self._attempt_reconnect()
                    if not self._check_socket_health():
                        self._handle_error(cmd.id, cmd.device_type, ModbusError('send_failed', 'Socket unhealthy'))
                        continue
                
                # Respect minimum command interval
                time_since_last = time.time() - self._last_command_time
                if time_since_last < self._command_interval:
                    time.sleep(self._command_interval - time_since_last)
                
                try:
                    if self.is_connected and self.socket:
                        with port_lock:
                            # Add CRC and send
                            command_with_crc = cmd.data + self.calculate_crc16(cmd.data)
                            self.socket.sendall(command_with_crc)
                            
                            # Update stats
                            self._last_command_time = time.time()
                            cmd.timestamp = self._last_command_time
                            self.stats['commands_sent'] += 1
                            self.stats['last_command_time'] = self._last_command_time
                    else:
                        self.stats['commands_failed'] += 1
                        self._handle_error(cmd.id, cmd.device_type, ModbusError('send_failed', 'Socket not connected'))
                except Exception as e:
                    self.stats['commands_failed'] += 1
                    self._handle_error(cmd.id, cmd.device_type, ModbusError('send_failed', str(e)))
                
                self.command_queue.task_done()
                
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error in command processor: {str(e)}")
                if 'cmd' in locals():
                    self._handle_error(cmd.id, cmd.device_type, ModbusError('error', str(e)))

    def _read_responses(self) -> None:
        """Read and process responses from the server with integrated cleanup and health checks."""
        buffer = {}  # Separate buffer for each port
        last_cleanup_time = time.time()
        last_health_check = time.time()
        
        while self._running:
            if not self.is_connected:
                time.sleep(0.1)
                continue

            current_time = time.time()
            
            # Periodic cleanup of timed-out commands
            if current_time - last_cleanup_time > Config.CLEANUP_INTERVAL:
                self._cleanup_timed_out_commands()
                last_cleanup_time = current_time
            
            # Periodic health check
            if current_time - last_health_check > Config.HEALTH_CHECK_INTERVAL:
                self._check_connection_health_and_reconnect()
                last_health_check = current_time

            try:
                # First check for data without lock
                ready = select.select([self.socket], [], [], Config.READ_TIMEOUT)
                if not ready[0]:
                    continue

                # Read data and determine port
                data = self.socket.recv(256).decode()
                if not data:
                    raise ConnectionError("Connection lost")
                
                # Extract port from response (simplified)
                try:
                    port = data.split('_')[0]
                    if not port:
                        continue
                except (IndexError, AttributeError):
                    continue
                port_lock = self._get_port_lock(port)
                
                with port_lock:  # Use port-specific lock
                    if port not in buffer:
                        buffer[port] = ""
                    buffer[port] += data
                    
                    # Process complete responses for this port
                    while '\n' in buffer[port]:
                        line, buffer[port] = buffer[port].split('\n', 1)
                        if line.strip():
                            self._handle_response_line(line.strip())

            except socket.timeout:
                continue
            except Exception as e:
                logger.error(f"Error reading response: {str(e)}")
                self._attempt_reconnect()

    def _handle_response_line(self, response: str) -> None:
        """Process a single response line from the server."""
        try:
            if not response.strip():
                return
            
            parts = response.split(':')
            if len(parts) < 2:
                return
            
            response_id = parts[0]
            if not response_id:
                return
            
            if response_id in self.pending_commands:
                cmd = self.pending_commands[response_id]
                timestamp = float(parts[-1]) if len(parts) >= 3 else time.time()
                
                if 'ERROR' in parts[1]:
                    error_type = parts[2] if len(parts) >= 4 else 'unknown_error'
                    self._handle_error(response_id, cmd.device_type, ModbusError(error_type, f'Server error: {error_type}'))
                else:
                    try:
                        response_bytes = bytes.fromhex(parts[1]) if parts[1] else None
                        self.event_emitter.emit_response(ModbusResponse(
                            command_id=response_id,
                            data=response_bytes,
                            device_type=cmd.device_type,
                            status='success',
                            timestamp=timestamp
                        ))
                        self.stats['responses_received'] += 1
                        self.stats['last_response_time'] = time.time()
                    except ValueError:
                        self._handle_error(response_id, cmd.device_type, ModbusError('invalid_response', 'Invalid response format'))
                
                del self.pending_commands[response_id]
                
        except Exception as e:
            logger.error(f"Error handling response: {str(e)}")

    def _cleanup_timed_out_commands(self) -> None:
        """Clean up timed-out pending commands (O(n) operation)."""
        try:
            current_time = time.time()
            
            # Clean up timed-out commands (O(n) - no sorting)
            timed_out = []
            for cmd_id, cmd in self.pending_commands.items():
                if cmd.timestamp > 0 and (current_time - cmd.timestamp) > (cmd.timeout + 0.5):
                    timed_out.append(cmd_id)
            
            for cmd_id in timed_out:
                cmd = self.pending_commands[cmd_id]
                self.stats['timeouts'] += 1
                self._handle_error(cmd_id, cmd.device_type, ModbusError('timeout', 'Command timed out'))
                del self.pending_commands[cmd_id]
            
            # Simple memory management (O(1) check)
            if len(self.pending_commands) > Config.MAX_PENDING_COMMANDS:
                # Remove oldest commands (simple approach)
                oldest_cmd_id = min(self.pending_commands.keys(), key=lambda k: self.pending_commands[k].timestamp)
                del self.pending_commands[oldest_cmd_id]
                    
        except Exception as e:
            logger.error(f"Error in cleanup: {str(e)}")

    def _check_connection_health(self) -> bool:
        """Real connection health check."""
        try:
            if not self.socket or not self.is_connected:
                return False
            
            # Send ping command to server
            ping_data = b'PING\n'
            self.socket.send(ping_data)
            
            # Wait for pong response
            ready = select.select([self.socket], [], [], 1.0)
            if ready[0]:
                response = self.socket.recv(10)
                return response == b'PONG\n'
            
            return False
        except Exception as e:
            logger.debug(f"Health check failed: {str(e)}")
            return False

    def _check_connection_health_and_reconnect(self) -> None:
        """Check connection health and reconnect if necessary (integrated into read thread)."""
        try:
            if not self.is_connected and self._host and self._port:
                logger.info("Health check: attempting to reconnect...")
                self.stats['reconnections'] += 1
                self._establish_connection()
            elif self.is_connected and not self._check_connection_health():
                logger.warning("Health check failed, reconnecting...")
                self.stats['reconnections'] += 1
                self._establish_connection()
        except Exception as e:
            logger.debug(f"Health check reconnection failed: {str(e)}")

    def _handle_error(self, command_id: str, device_type: str, error: ModbusError) -> None:
        """Unified error handling for all error types."""
        self.stats['errors'] = self.stats.get('errors', 0) + 1
        self.event_emitter.emit_response(ModbusResponse(
            command_id=command_id,
            data=None,
            device_type=device_type,
            status=error.error_type,
            timestamp=time.time()
        ))

    def _emit_error_response(self, command_id: str, device_type: str, status: str, timestamp: float = None) -> None:
        """Helper method to emit error responses (backward compatibility)."""
        error = ModbusError(status, f"Error: {status}", command_id)
        self._handle_error(command_id, device_type, error)

    def _attempt_reconnect(self) -> None:
        """Modified to use exponential backoff and maintain connection details"""
        if not self.is_connected or not self._host or not self._port:
            return
        
        self.is_connected = False
        try:
            self.socket.close()
        except:
            pass

        retry_count = 0
        max_retries = 3
        
        while self._running and not self.is_connected and retry_count < max_retries:
            try:
                logger.info(f"Attempting to reconnect (attempt {retry_count + 1}/{max_retries})...")
                self._establish_connection()
                break
            except Exception as e:
                retry_count += 1
                logger.info(f"Reconnection failed: {str(e)}")
                time.sleep(min(5 * retry_count, 15))

    def stop(self) -> None:
        """Stop the client and cleanup resources."""
        self._running = False
        self.event_emitter.stop()
        
        with self._socket_lock:
            if self.socket:
                try:
                    self.socket.close()
                except:
                    pass
        
        # Wait for threads to finish
        for thread in self._threads.values():
            if thread.is_alive():
                thread.join(timeout=1.0)
        
        # Clear queues
        while not self.command_queue.empty():
            try:
                self.command_queue.get_nowait()
                self.command_queue.task_done()
            except queue.Empty:
                break

    # Removed _handle_command_error (replaced by _handle_error)

    def get_health_status(self) -> dict:
        """Get client health status and statistics."""
        current_time = time.time()
        
        # Update thread health
        self.thread_health['command_thread_alive'] = self._threads['command'].is_alive()
        self.thread_health['read_thread_alive'] = self._threads['read'].is_alive()
        self.thread_health['last_health_check'] = current_time
        
        return {
            'is_connected': self.is_connected,
            'pending_commands': len(self.pending_commands),
            'queue_size': self.command_queue.qsize(),
            'stats': self.stats.copy(),
            'thread_health': self.thread_health.copy(),
            'uptime': current_time - getattr(self, '_start_time', current_time),
            'last_command_age': current_time - self.stats['last_command_time'] if self.stats['last_command_time'] > 0 else None,
            'last_response_age': current_time - self.stats['last_response_time'] if self.stats['last_response_time'] > 0 else None,
            'success_rate': (
                self.stats['responses_received'] / max(self.stats['commands_sent'], 1) * 100
                if self.stats['commands_sent'] > 0 else 0
            )
        }

