import logging
import time
import socket
import threading
import queue
from typing import Dict, List
import random
import string

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from lumina_modbus_event_emitter import ModbusEventEmitter, ModbusResponse

class LuminaModbusClient:
    def __init__(self):
        # Basic initialization
        self.socket = None
        self.is_connected = False
        self.event_emitter = ModbusEventEmitter()
        
        # Threading components
        self._running = True
        self.command_queue = queue.Queue()
        self.recent_commands: Dict[str, dict] = {}
        self._lock = threading.Lock()
        
        # Connection details
        self._host = None
        self._port = None
        
        # Start worker threads
        self._command_thread = threading.Thread(target=self._process_commands, daemon=True)
        self._read_thread = threading.Thread(target=self._read_responses, daemon=True)
        self._cleanup_thread = threading.Thread(target=self._cleanup_recent_commands, daemon=True)
        self._watchdog_thread = threading.Thread(target=self._connection_watchdog, daemon=True)
        
        self._command_thread.start()
        self._read_thread.start()
        self._cleanup_thread.start()
        self._watchdog_thread.start()

    def connect(self, host='127.0.0.1', port=8888):
        self._host = host
        self._port = port
        self._establish_connection()

    def _establish_connection(self):
        """Internal method to establish the socket connection"""
        try:
            if self.socket:
                try:
                    self.socket.close()
                except:
                    pass
            
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # Set TCP keepalive parameters (if supported by OS)
            try:
                self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)  # Start sending keepalive after 30 seconds
                self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)  # Send keepalive every 5 seconds
                self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)    # Drop connection after 3 failed keepalives
            except AttributeError:
                pass  # OS doesn't support these options
            
            self.socket.connect((self._host, self._port))
            self.socket.settimeout(1.0)  # 1 second timeout
            self.is_connected = True
            logger.info(f"Connected to server at {self._host}:{self._port}")
        except Exception as e:
            logger.error(f"Failed to connect: {str(e)}")
            self.is_connected = False
            raise

    def _connection_watchdog(self):
        """Monitors connection health and reconnects if necessary"""
        while self._running:
            if not self.is_connected and self._host and self._port:
                try:
                    logger.info("Watchdog attempting to reconnect...")
                    self._establish_connection()
                except Exception as e:
                    logger.error(f"Watchdog reconnection failed: {str(e)}")
                    time.sleep(5)  # Wait before retry
            time.sleep(1)  # Check connection every second

    def send_command(self, device_type: str, port: str, command: bytes, **kwargs) -> str:
        # Truncate the hex command if it's longer than 12 characters
        truncated_hex = command.hex()[:12] if len(command.hex()) > 12 else command.hex()
        # Generate random 2-character alphanumeric string
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
            # response_length should include the 2 CRC bytes from the sensor
            str(kwargs.get('response_length', 0))
        ]
        
        # Add optional timeout if specified
        if 'timeout' in kwargs:
            message_parts.append(str(kwargs['timeout']))
        
        # Join parts with ':' and add newline
        command_str = ':'.join(message_parts) + '\n'
        
        self.command_queue.put({
            'id': command_id,
            'device_type': device_type,
            'command': command_str.encode(),
            'kwargs': kwargs
        })
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
        while self._running:
            try:
                command = self.command_queue.get(timeout=0.1)
                if self.is_connected and self.socket:
                    with self._lock:
                        # Send the command string directly (already properly formatted)
                        self.socket.sendall(command['command'])
                        
                        # Store command info for response handling
                        self.recent_commands[command['id']] = {
                            'timestamp': time.time(),
                            'device_type': command['device_type'],
                            'response_length': command['kwargs'].get('response_length', 0),
                            'timeout': command['kwargs'].get('timeout', 1.0)
                        }
                        
                    logger.debug(f"Sent command: {command['command'].decode().strip()}")
                else:
                    logger.error("Socket not connected")
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error processing command: {str(e)}")
                self._attempt_reconnect()

    def _read_responses(self) -> None:
        buffer = ""
        while self._running:
            try:
                if not self.is_connected:
                    time.sleep(0.1)
                    continue

                try:
                    with self._lock:
                        # Reduced buffer size and using a longer timeout
                        self.socket.settimeout(5.0)  # 5 second timeout for reads
                        data = self.socket.recv(256).decode()
                        if not data:
                            raise ConnectionError("Connection lost - empty data received")
                        buffer += data
                except socket.timeout:
                    # Timeout is normal, continue listening
                    continue
                except (ConnectionError, OSError) as e:
                    logger.error(f"Connection error while reading: {str(e)}")
                    self._attempt_reconnect()
                    self._handle_pending_commands_on_disconnect()
                    continue

                # Process complete messages from buffer
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    if line.strip():  # Only process non-empty lines
                        self._handle_response(line.strip())

            except Exception as e:
                logger.error(f"Error reading response: {str(e)}")
                self._attempt_reconnect()

    def _handle_response(self, response: str) -> None:
        try:
            response_parts = response.split(':', 1)
            if len(response_parts) < 2:
                logger.warning(f"Invalid response format: {response}")
                return

            response_uuid = response_parts[0]
            response_data = response_parts[1]

            with self._lock:
                if response_uuid in self.recent_commands:
                    command_info = self.recent_commands[response_uuid]
                    
                    # Handle different response types
                    if response_data in ['MISMATCH', 'TIMEOUT', 'ERROR', 'QUEUE_FULL', 'REQUEST_EXPIRED']:
                        logger.warning(f"Received {response_data} for command {response_uuid}")
                        self.event_emitter.emit_response(ModbusResponse(
                            command_id=response_uuid,
                            data=None,
                            device_type=command_info['device_type'],
                            status=response_data.lower()
                        ))
                    else:
                        try:
                            response_bytes = bytes.fromhex(response_data) if response_data else None
                            logger.debug(f"Received matching response for command {response_uuid}")
                            self.event_emitter.emit_response(ModbusResponse(
                                command_id=response_uuid,
                                data=response_bytes,
                                device_type=command_info['device_type'],
                                status='success'
                            ))
                        except ValueError as e:
                            logger.error(f"Invalid hex data in response: {response_data}")
                            self.event_emitter.emit_response(ModbusResponse(
                                command_id=response_uuid,
                                data=None,
                                device_type=command_info['device_type'],
                                status='invalid_response'
                            ))
                    
                    del self.recent_commands[response_uuid]
                else:
                    logger.warning(f"Received unmatched response: {response}")
        except Exception as e:
            logger.error(f"Error handling response: {str(e)}", exc_info=True)

    def _cleanup_recent_commands(self) -> None:
        while self._running:
            try:
                current_time = time.time()
                with self._lock:
                    self.recent_commands = {
                        uuid: info for uuid, info in self.recent_commands.items()
                        if current_time - info['timestamp'] <= 10
                    }
                time.sleep(1)
            except Exception as e:
                logger.error(f"Error in cleanup: {str(e)}")

    def _check_timeouts(self) -> None:
        current_time = time.time()
        with self._lock:
            timed_out = [
                uuid for uuid, info in self.recent_commands.items()
                if current_time - info['timestamp'] > info['timeout']
            ]
            for uuid in timed_out:
                command_info = self.recent_commands[uuid]
                logger.warning(f"Command timed out: {uuid}")
                self.event_emitter.emit_response(ModbusResponse(
                    command_id=uuid,
                    data=None,
                    device_type=command_info['device_type'],
                    status='timeout'
                ))
                del self.recent_commands[uuid]

    def _handle_pending_commands_on_disconnect(self) -> None:
        """Handle any commands that were in flight when connection dropped"""
        with self._lock:
            for uuid, command_info in self.recent_commands.items():
                logger.warning(f"Connection dropped with pending command: {uuid}")
                self.event_emitter.emit_response(ModbusResponse(
                    command_id=uuid,
                    data=None,
                    device_type=command_info['device_type'],
                    status='connection_lost'
                ))
            self.recent_commands.clear()

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
                logger.error(f"Reconnection failed: {str(e)}")
                time.sleep(min(5 * retry_count, 15))

    def stop(self) -> None:
        self._running = False
        self.event_emitter.stop()
        if self.socket:
            try:
                self.socket.close()
            except:
                pass

