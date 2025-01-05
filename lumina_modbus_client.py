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

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from lumina_modbus_event_emitter import ModbusEventEmitter, ModbusResponse

@dataclass
class PendingCommand:
    id: str
    device_type: str
    timestamp: float
    response_length: int
    timeout: float

class LuminaModbusClient:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

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
        
        # Connection details
        self._host = None
        self._port = None
        self._reconnect_attempts = reconnect_attempts
        self._last_command_time = 0
        self._command_interval = 0.01  # Minimum time between commands
        
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
                self.socket.settimeout(1.0)
                self.is_connected = True
                logger.info(f"Connected to server at {self._host}:{self._port}")
                return True
                
        except Exception as e:
            logger.info(f"Failed to connect: {str(e)}")
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
            self.command_queue.put({
                'id': command_id,
                'device_type': device_type,
                'command': command_str.encode(),
                'kwargs': kwargs
            }, timeout=1.0)
            
            # Store pending command info
            self.pending_commands[command_id] = PendingCommand(
                id=command_id,
                device_type=device_type,
                timestamp=time.time(),
                response_length=kwargs.get('response_length', 0),
                timeout=kwargs.get('timeout', 1.0)
            )
            
            return command_id
            
        except queue.Full:
            logger.info("Command queue full, command dropped")
            self.event_emitter.emit_response(ModbusResponse(
                command_id=command_id,
                data=None,
                device_type=device_type,
                status='queue_full'
            ))
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

    def _process_commands(self) -> None:
        """Process commands from the queue and send to server."""
        while self._running:
            try:
                command = self.command_queue.get(timeout=0.1)
                
                # Respect minimum command interval
                time_since_last = time.time() - self._last_command_time
                if time_since_last < self._command_interval:
                    time.sleep(self._command_interval - time_since_last)
                
                if self.is_connected and self.socket:
                    with self._socket_lock:
                        self.socket.sendall(command['command'])
                        self._last_command_time = time.time()
                        logger.info(f"Sent command: {command['command'].decode().strip()}")
                else:
                    self._handle_command_error(command['id'], command['device_type'], 'not_connected')
                
                self.command_queue.task_done()
                
            except queue.Empty:
                continue
            except Exception as e:
                logger.info(f"Error processing command: {str(e)}")
                if 'command' in locals():
                    self._handle_command_error(command['id'], command['device_type'], 'error')

    def _read_responses(self) -> None:
        """Read and process responses from the server."""
        buffer = ""
        while self._running:
            if not self.is_connected:
                time.sleep(0.1)
                continue

            try:
                with self._socket_lock:
                    self.socket.settimeout(5.0)
                    data = self.socket.recv(256).decode()
                    if not data:
                        raise ConnectionError("Connection lost")
                    buffer += data

                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    if line.strip():
                        self._handle_response_line(line.strip())

            except socket.timeout:
                continue
            except Exception as e:
                logger.info(f"Error reading response: {str(e)}")
                self._attempt_reconnect()

    def _handle_response_line(self, response: str) -> None:
        """Process a single response line from the server."""
        try:
            response_id, response_data = response.split(':', 1)
            
            if response_id in self.pending_commands:
                command_info = self.pending_commands[response_id]
                
                if response_data in ['MISMATCH', 'TIMEOUT', 'ERROR', 'QUEUE_FULL']:
                    self._emit_error_response(response_id, command_info.device_type, response_data.lower())
                else:
                    try:
                        response_bytes = bytes.fromhex(response_data) if response_data else None
                        self.event_emitter.emit_response(ModbusResponse(
                            command_id=response_id,
                            data=response_bytes,
                            device_type=command_info.device_type,
                            status='success'
                        ))
                    except ValueError:
                        self._emit_error_response(response_id, command_info.device_type, 'invalid_response')
                
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
                    if current_time - cmd.timestamp > cmd.timeout
                ]
                
                for cmd_id in timed_out:
                    cmd_info = self.pending_commands[cmd_id]
                    self._emit_error_response(cmd_id, cmd_info.device_type, 'timeout')
                    del self.pending_commands[cmd_id]
                
                time.sleep(0.1)
            except Exception as e:
                logger.info(f"Error in command cleanup: {str(e)}")

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

    def _emit_error_response(self, command_id: str, device_type: str, status: str) -> None:
        """Helper method to emit error responses."""
        self.event_emitter.emit_response(ModbusResponse(
            command_id=command_id,
            data=None,
            device_type=device_type,
            status=status
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

