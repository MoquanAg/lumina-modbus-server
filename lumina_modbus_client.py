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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from lumina_modbus_event_emitter import ModbusEventEmitter, ModbusResponse

@dataclass
class PendingCommand:
    id: str
    device_type: str
    timestamp: float
    response_length: int
    timeout: float

@dataclass
class ModbusResponse:
    command_id: str
    data: Optional[bytes]
    device_type: str
    status: str
    timestamp: float = 0.0  # Add timestamp field with default value

class LuminaModbusClient:
    _instances = {}  # Change from single instance to per-process instances
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        process_id = os.getpid()  # Get current process ID
        with cls._lock:
            if process_id not in cls._instances:
                cls._instances[process_id] = super().__new__(cls)
                cls._instances[process_id]._initialized = False
            return cls._instances[process_id]

    def __init__(self, reconnect_attempts: int = 3, command_queue_size: int = 1000):
        if self._initialized:
            return
            
        # Basic initialization
        self.socket = None
        self.is_connected = False
        self.event_emitter = ModbusEventEmitter()
        
        # Threading components
        self._running = True
        self.command_queue = queue.Queue(maxsize=command_queue_size)
        self.pending_commands: Dict[str, PendingCommand] = {}
        self._socket_lock = threading.Lock()
        self._port_locks = {}  # Dict to store locks for each port
        self._send_locks = {}  # Dict for send locks per port
        self._recv_locks = {}  # Dict for receive locks per port
        
        # Connection details
        self._host = None
        self._port = None
        self._reconnect_attempts = reconnect_attempts
        self._last_command_time = 0
        self._command_interval = 0.001  # Reduce to 1ms
        
        # Start worker threads
        self._threads = {
            'command': threading.Thread(target=self._process_commands, name="CommandProcessor", daemon=True),
            'read': threading.Thread(target=self._read_responses, name="ResponseReader", daemon=True),
            'cleanup': threading.Thread(target=self._cleanup_pending_commands, name="CommandCleaner", daemon=True),
            'watchdog': threading.Thread(target=self._connection_watchdog, name="ConnectionWatchdog", daemon=True),
            'monitor': threading.Thread(target=self._monitor_health, name="HealthMonitor", daemon=True)
        }
        
        for thread in self._threads.values():
            thread.start()
        
        self._initialized = True
        logger.info("LuminaModbusClient initialized")
        
        self.request_times = {}  # Add dictionary to track request creation times

    def connect(self, host='127.0.0.1', port=8888):
        """Connect to the Modbus server."""
        self._host = host
        self._port = port
        return self._establish_connection()

    def _establish_connection(self) -> bool:
        """Internal method to establish the socket connection."""
        try:
            with self._socket_lock:
                if self.socket:
                    try:
                        self.socket.close()
                        logger.debug("Closed existing socket")
                    except:
                        pass
                
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
                self.socket.settimeout(5.0)
                self.is_connected = True
                logger.debug(f"Socket connected and timeout set to 5.0 seconds")
                logger.info(f"Connected to server at {self._host}:{self._port}")
                return True
                
        except Exception as e:
            logger.error(f"Failed to connect: {str(e)}")
            self.is_connected = False
            return False

    def send_command(self, device_type: str, port: str, command: bytes, **kwargs) -> str:
        """
        Queue a command to be sent to the server.
        
        Args:
            device_type: Type of device (e.g., 'THC', 'EC', etc.)
            port: Serial port to use
            command: Command bytes to send
            **kwargs: Additional arguments (baudrate, response_length, timeout)
        
        Returns:
            str: Command ID for tracking the response
        """
        # Generate unique command ID
        truncated_hex = command.hex()[:12]
        random_suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=2))
        port_name = port.split("/")[-1]
        send_time = time.strftime('%Y%m%d%H%M%S')
        command_id = f"{port_name}_{device_type}_{truncated_hex}_{send_time}_{random_suffix}"
        
        # Store creation time
        self.request_times[command_id] = time.time()
        
        # Add CRC to command
        command_with_crc = command + self.calculate_crc16(command)
        
        # Format message parts
        message_parts = [
            command_id,
            device_type,
            port,
            str(kwargs.get('baudrate', 9600)),
            command_with_crc.hex(),
            str(kwargs.get('response_length', 0))
        ]
        
        if 'timeout' in kwargs:
            message_parts.append(str(kwargs['timeout']))
        
        command_str = ':'.join(message_parts) + '\n'
        
        try:
            logger.debug(f"Queueing command - ID: {command_id}, Device: {device_type}")
            
            self.command_queue.put({
                'id': command_id,
                'device_type': device_type,
                'command': command_str.encode(),
                'kwargs': kwargs,
                'timeout': kwargs.get('timeout', 5.0)  # Use command-specific timeout or default to 5.0
            }, timeout=1.0)
            
            logger.debug(f"Command queued successfully - ID: {command_id}")
            
            # Initialize PendingCommand with the command-specific timeout
            self.pending_commands[command_id] = PendingCommand(
                id=command_id,
                device_type=device_type,
                timestamp=0,  # Will be set when command is actually sent
                response_length=kwargs.get('response_length', 0),
                timeout=kwargs.get('timeout', 5.0)  # Use command-specific timeout or default to 5.0
            )
            
            return command_id
            
        except queue.Full:
            logger.error(f"Command queue full, dropping command - ID: {command_id}")
            self._emit_error_response(command_id, device_type, 'queue_full')
            return command_id

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
        if not self.socket:
            return False
        try:
            # Try to check socket state
            return self.socket.fileno() != -1 and self.is_connected
        except Exception:
            return False

    def _get_port_locks(self, port: str):
        """Get or create locks for a specific port"""
        if port not in self._port_locks:
            self._port_locks[port] = threading.Lock()
            self._send_locks[port] = threading.Lock()
            self._recv_locks[port] = threading.Lock()
        return (self._port_locks[port], self._send_locks[port], self._recv_locks[port])

    def _process_commands(self) -> None:
        """Process commands from the queue and send to server."""
        while self._running:
            try:
                command = self.command_queue.get(timeout=0.1)
                port = command['command'].decode().split(':')[2]  # Extract port from command string
                _, send_lock, _ = self._get_port_locks(port)
                logger.debug(f"Processing command from queue - ID: {command['id']}")
                
                # Check socket health before sending
                if not self._check_socket_health():
                    logger.error(f"Socket unhealthy before sending command {command['id']}")
                    self._attempt_reconnect()
                    if not self._check_socket_health():
                        self._handle_command_error(command['id'], command['device_type'], 'send_failed')
                        continue
                
                # Respect minimum command interval
                time_since_last = time.time() - self._last_command_time
                if time_since_last < self._command_interval:
                    time.sleep(self._command_interval - time_since_last)
                
                try:
                    if self.is_connected and self.socket:
                        with send_lock:  # Use port-specific send lock
                            logger.debug(f"Sending command to socket - ID: {command['id']}")
                            self.socket.sendall(command['command'])
                            send_time = time.time()
                            self._last_command_time = send_time
                            
                            # Update pending command timestamp when actually sent
                            if command['id'] in self.pending_commands:
                                self.pending_commands[command['id']].timestamp = send_time
                                logger.debug(f"Updated timestamp for command {command['id']} to {send_time}")
                            
                            logger.info(f"Successfully sent command - ID: {command['id']}")
                    else:
                        logger.error(f"Socket not connected, cannot send command - ID: {command['id']}")
                        self._handle_command_error(command['id'], command['device_type'], 'send_failed')
                except Exception as e:
                    logger.error(f"Failed to send command {command['id']}: {str(e)}")
                    self._handle_command_error(command['id'], command['device_type'], 'send_failed')
                
                self.command_queue.task_done()
                
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error in command processor: {str(e)}")
                if 'command' in locals():
                    self._handle_command_error(command['id'], command['device_type'], 'error')

    def _read_responses(self) -> None:
        """Read and process responses from the server."""
        buffer = {}  # Separate buffer for each port
        while self._running:
            if not self.is_connected:
                time.sleep(0.1)
                continue

            try:
                # First check for data without lock
                ready = select.select([self.socket], [], [], 0.01)
                if not ready[0]:
                    continue

                # Read data and determine port
                data = self.socket.recv(256).decode()
                if not data:
                    raise ConnectionError("Connection lost")
                
                # Extract port from response (assuming it's in the command ID format)
                port = data.split('_')[0]  # First part of command ID is port name
                _, _, recv_lock = self._get_port_locks(port)
                
                with recv_lock:  # Use port-specific receive lock
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
            parts = response.split(':')
            if len(parts) < 2:
                return
            
            response_id = parts[0]
            
            # Calculate total time if we have the creation time
            if response_id in self.request_times:
                total_time = time.time() - self.request_times[response_id]
                logger.info(f"Request {response_id} took {total_time:.3f} seconds")
                del self.request_times[response_id]  # Cleanup
            
            if response_id in self.pending_commands:
                command_info = self.pending_commands[response_id]
                
                # Extract timestamp from response (use server timestamp if available)
                timestamp = float(parts[-1]) if len(parts) >= 3 else time.time()
                
                if 'ERROR' in parts[1]:
                    error_type = parts[2] if len(parts) >= 4 else 'unknown_error'
                    self._emit_error_response(response_id, command_info.device_type, error_type, timestamp)
                else:
                    try:
                        response_bytes = bytes.fromhex(parts[1]) if parts[1] else None
                        self.event_emitter.emit_response(ModbusResponse(
                            command_id=response_id,
                            data=response_bytes,
                            device_type=command_info.device_type,
                            status='success',
                            timestamp=timestamp
                        ))
                    except ValueError:
                        self._emit_error_response(response_id, command_info.device_type, 'invalid_response', timestamp)
                
                del self.pending_commands[response_id]
            else:
                logger.warning(f"Received response for unknown command: {response_id}")
                
        except Exception as e:
            logger.info(f"Error handling response line: {str(e)}")

    def _cleanup_pending_commands(self) -> None:
        """Clean up timed-out pending commands."""
        while self._running:
            try:
                current_time = time.time()
                timed_out = [
                    cmd_id for cmd_id, cmd in self.pending_commands.items()
                    if cmd.timestamp > 0 and  # Only check commands that have been sent
                    (current_time - cmd.timestamp) > (cmd.timeout + 0.5)  # Add small buffer
                ]
                
                for cmd_id in timed_out:
                    cmd_info = self.pending_commands[cmd_id]
                    logger.warning(f"Command {cmd_id} timed out after {current_time - cmd_info.timestamp:.2f}s")
                    self._emit_error_response(cmd_id, cmd_info.device_type, 'timeout')
                    del self.pending_commands[cmd_id]
                
                time.sleep(0.5)  # Increased sleep time
            except Exception as e:
                logger.error(f"Error in command cleanup: {str(e)}")

    def _monitor_health(self) -> None:
        """Monitor client health metrics."""
        while self._running:
            try:
                queue_size = self.command_queue.qsize()
                pending_count = len(self.pending_commands)
                
                if queue_size > self.command_queue.maxsize * 0.8:
                    logger.warning(f"Command queue is {queue_size}/{self.command_queue.maxsize} full")
                if pending_count > 100:
                    logger.warning(f"High number of pending commands: {pending_count}")
                
                time.sleep(5)
            except Exception as e:
                logger.info(f"Error in health monitor: {str(e)}")

    def _emit_error_response(self, command_id: str, device_type: str, status: str, timestamp: float = None) -> None:
        """Helper method to emit error responses."""
        if timestamp is None:
            timestamp = time.time()
        
        self.event_emitter.emit_response(ModbusResponse(
            command_id=command_id,
            data=None,
            device_type=device_type,
            status=status,
            timestamp=timestamp  # Add timestamp to error responses
        ))

    def _connection_watchdog(self) -> None:
        """Monitors connection health and reconnects if necessary"""
        while self._running:
            if not self.is_connected and self._host and self._port:
                try:
                    logger.info("Watchdog attempting to reconnect...")
                    self._establish_connection()
                except Exception as e:
                    logger.info(f"Watchdog reconnection failed: {str(e)}")
                    time.sleep(5)  # Wait before retry
            time.sleep(1)  # Check connection every second

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

    def _handle_command_error(self, command_id: str, device_type: str, error_type: str) -> None:
        """Handle command errors by emitting appropriate error responses."""
        try:
            # Store command in pending_commands if not already there
            if command_id not in self.pending_commands:
                self.pending_commands[command_id] = PendingCommand(
                    id=command_id,
                    device_type=device_type,
                    timestamp=time.time(),
                    response_length=0,  # Not relevant for errors
                    timeout=1.0  # Default timeout
                )
            
            # Emit error response
            self._emit_error_response(command_id, device_type, error_type)
            
            # Clean up the pending command
            if command_id in self.pending_commands:
                del self.pending_commands[command_id]
            
        except Exception as e:
            logger.info(f"Error handling command error: {str(e)}")

