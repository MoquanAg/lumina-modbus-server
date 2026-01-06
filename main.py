"""
LuminaModbusServer: Upgraded with PyModbus for robust Modbus RTU communication.

This version replaces custom serial code with PyModbus while maintaining
the existing TCP server architecture and text protocol for full compatibility
with existing clients.

Key improvements:
- PyModbus handles timing, CRC, and retries
- More robust error handling
- Better Modbus compliance
- Same protocol - no client changes needed!
"""

import concurrent.futures
from queue import Queue
from typing import Dict, Optional, Tuple
import time
from dataclasses import dataclass
from LuminaLogger import LuminaLogger
import psutil
import os
import threading
import socket
import queue
import asyncio
import struct

# PyModbus imports
try:
    from pymodbus.client import AsyncModbusSerialClient
    PYMODBUS_AVAILABLE = True
except ImportError:
    PYMODBUS_AVAILABLE = False
    print("WARNING: PyModbus not installed. Install with: pip install pymodbus>=3.6.0")

AVAILABLE_PORTS = ['/dev/ttyAMA0', '/dev/ttyAMA1', '/dev/ttyAMA2', '/dev/ttyAMA3', '/dev/ttyAMA4']

@dataclass
class PyModbusConnection:
    """PyModbus client connection wrapper"""
    client: 'AsyncModbusSerialClient'
    port: str
    baudrate: int
    last_used: float
    event_loop: asyncio.AbstractEventLoop
    in_use: bool = False
    min_command_spacing: float = 0.05  # Will be set dynamically based on baudrate


def calculate_min_command_spacing(baudrate: int) -> float:
    """
    Calculate minimum command spacing based on baud rate.

    Lower baud rates need more time between commands due to:
    - Longer transmission times (1 byte ≈ 10 bits at ~1ms per bit at 9600 baud)
    - Device turnaround time (time for slave to process and respond)
    - Cable propagation delays (more significant with longer cables)

    Args:
        baudrate: Serial baud rate (e.g., 9600, 19200, 115200)

    Returns:
        Minimum spacing in seconds between commands on the same port
    """
    if baudrate <= 4800:
        return 0.25  # 250ms for very slow rates
    elif baudrate <= 9600:
        return 0.15  # 150ms for 9600 baud (safe for long cables up to 15m)
    elif baudrate <= 19200:
        return 0.10  # 100ms for 19200 baud
    elif baudrate <= 38400:
        return 0.08  # 80ms for 38400 baud
    elif baudrate <= 57600:
        return 0.06  # 60ms for 57600 baud
    else:
        return 0.05  # 50ms for high-speed (115200+)

class LuminaModbusServer:
    def __init__(self, host='127.0.0.1', port=8888, max_queue_size=100, request_timeout=30):
        if not PYMODBUS_AVAILABLE:
            raise RuntimeError("PyModbus is required but not installed. Run: pip install pymodbus>=3.6.0")
        
        # Server configuration
        self.host = host
        self.port = port
        self.request_timeout = request_timeout
        
        # Connection management
        self.clients = set()
        self.command_queues = {
            port: Queue(maxsize=max_queue_size) 
            for port in AVAILABLE_PORTS
        }
        
        # PyModbus connection pool (one client per port/baudrate combination)
        self.pymodbus_connections: Dict[str, Dict[int, PyModbusConnection]] = {
            port: {} for port in AVAILABLE_PORTS
        }
        
        # Thread pool for serial operations
        self.thread_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(AVAILABLE_PORTS),
            thread_name_prefix="serial_worker"
        )
        
        # Logging setup
        self.logger = LuminaLogger('LuminaModbusServer')
        self.port_loggers = {
            port: LuminaLogger(f'{port.split("/")[-1]}')
            for port in AVAILABLE_PORTS
        }
        
        # Client tracking
        self.client_pending_commands = {}
        self.client_port_initialized = {}  # {client_id: set of (port, baudrate) tuples}

        # Shutdown event
        self.shutdown_event = threading.Event()
        
        self.logger.info("PyModbus-enabled Lumina Modbus Server initialized")

    def start(self):
        """Start the Modbus server and initialize all components."""
        try:
            # Start serial processors in thread pool
            self.logger.info("Starting serial processors with PyModbus...")
            
            self.processor_threads = []
            
            for port in AVAILABLE_PORTS:
                self.logger.info(f"Starting processor for {port}")
                thread = threading.Thread(
                    target=self.process_serial_port,
                    args=(port,),
                    name=f"serial_processor_{port}"
                )
                thread.daemon = True
                thread.start()
                self.processor_threads.append(thread)
                
            # Start TCP server in main thread
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(5)
            
            self.logger.info(f"Server started on {self.host}:{self.port} (PyModbus mode)")
            
            while not self.shutdown_event.is_set():
                try:
                    client_socket, address = self.server_socket.accept()
                    client_thread = threading.Thread(
                        target=self.handle_client,
                        args=(client_socket, address)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except Exception as e:
                    if not self.shutdown_event.is_set():
                        self.logger.error(f"Error accepting client: {e}")
                        
        except Exception as e:
            self.logger.error(f"Server startup failed: {str(e)}", exc_info=True)
            raise

    def handle_client(self, client_socket, address):
        """Handle incoming client connections (same as original)"""
        client_id = id(client_socket)
        self.logger.info(f"New client connected: {client_id} from {address}")
        
        # PyModbus connections are shared across all clients - no need to close them
        self.clients.add(client_id)
        self.client_pending_commands[client_id] = set()
        
        try:
            buffer = ""
            while not self.shutdown_event.is_set():
                try:
                    data = client_socket.recv(1024).decode()
                    if not data:
                        self.logger.info(f"Client {client_id} closed connection gracefully")
                        break
                    
                    buffer += data
                    while '\n' in buffer:
                        message, buffer = buffer.split('\n', 1)
                        self.process_client_message(client_id, message.strip(), client_socket)
                except (ConnectionResetError, ConnectionError, socket.error) as e:
                    self.logger.info(f"Client {client_id} connection error: {e}")
                    break
                    
        except Exception as e:
            self.logger.error(f"Error handling client {client_id}: {str(e)}")
        finally:
            self.cleanup_client(client_id, client_socket)

    def process_client_message(self, client_id: int, message: str, client_socket):
        """Parse client message and queue command for processing"""
        try:
            # Protocol: command_id:device_type:port:baudrate:command_hex:response_length:timeout
            parts = message.split(':')
            if len(parts) < 7:
                self.logger.error(f"Invalid message format from client {client_id}: {message}")
                return
            
            command_id, device_type, port, baudrate, command_hex, response_length, timeout = parts[:7]
            
            # Validate port
            if port not in AVAILABLE_PORTS:
                error_msg = f"Invalid port: {port}"
                self.logger.error(error_msg)
                self.send_error_sync({
                    'client_id': client_id,
                    'command_id': command_id,
                    'socket': client_socket
                }, error_msg)
                return
            
            # Parse command
            try:
                command_bytes = bytes.fromhex(command_hex)
                baudrate_int = int(baudrate)
                response_length_int = int(response_length)
                timeout_float = float(timeout)
            except (ValueError, TypeError) as e:
                error_msg = f"Invalid command parameters: {e}"
                self.logger.error(error_msg)
                self.send_error_sync({
                    'client_id': client_id,
                    'command_id': command_id,
                    'socket': client_socket
                }, error_msg)
                return
            
            # Queue command for processing
            command_info = {
                'client_id': client_id,
                'command_id': command_id,
                'device_type': device_type,
                'command': command_bytes,
                'baudrate': baudrate_int,
                'response_length': response_length_int,
                'timeout': timeout_float,
                'socket': client_socket,
                'queued_at': time.time()
            }
            
            self.command_queues[port].put(command_info, timeout=1.0)
            self.client_pending_commands[client_id].add(command_id)
            
        except queue.Full:
            self.logger.error(f"Command queue full for {port}")
            self.send_error_sync(command_info, "queue_full")
        except Exception as e:
            self.logger.error(f"Error processing client message: {str(e)}")

    def process_serial_port(self, port: str):
        """Process commands for a specific serial port using PyModbus"""
        port_logger = self.port_loggers[port]
        port_logger.info(f"PyModbus serial processor started for {port}")
        
        # Create dedicated event loop for this port's PyModbus operations
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        while not self.shutdown_event.is_set():
            try:
                command_info = None
                try:
                    command_info = self.command_queues[port].get(timeout=1.0)
                except queue.Empty:
                    continue
                
                # Check if client is still connected
                client_id = command_info['client_id']
                if client_id not in self.clients:
                    port_logger.debug(f"Skipping command for disconnected client {client_id}")
                    self.command_queues[port].task_done()
                    continue

                # Check if command has exceeded its timeout (client already gave up)
                command_age = time.time() - command_info['queued_at']
                if command_age > command_info['timeout']:
                    port_logger.debug(
                        f"Skipping stale command {command_info['command_id']} "
                        f"(age: {command_age:.2f}s > timeout: {command_info['timeout']:.2f}s)"
                    )
                    self.command_queues[port].task_done()
                    continue

                # Process command using PyModbus
                try:
                    response = loop.run_until_complete(
                        self.execute_pymodbus_command(port, command_info, loop)
                    )
                    
                    if client_id in self.clients:
                        self.send_response_sync(command_info, response)
                except Exception as e:
                    port_logger.error(f"Error processing PyModbus command: {str(e)}")
                    # Flush buffer after errors to clear late responses
                    self._flush_connected_serial_buffer(port, command_info['baudrate'])
                    if client_id in self.clients:
                        self.send_error_sync(command_info, str(e))
                finally:
                    if command_info:
                        try:
                            self.command_queues[port].task_done()
                        except ValueError:
                            port_logger.debug("Task already marked as done")
                    
            except Exception as e:
                port_logger.error(f"Critical error in PyModbus processor: {str(e)}")
                time.sleep(0.1)
        
        # Clean up event loop
        loop.close()

    async def execute_pymodbus_command(self, port: str, command_info: dict, loop: asyncio.AbstractEventLoop) -> bytes:
        """
        Execute Modbus command using PyModbus.
        
        Parses raw Modbus frame and uses PyModbus for robust communication.
        """
        port_logger = self.port_loggers[port]
        baudrate = command_info['baudrate']
        command = command_info['command']
        
        try:
            # Parse Modbus frame
            # Format: [slave_addr, function_code, data..., crc_low, crc_high]
            if len(command) < 4:
                raise ValueError(f"Modbus frame too short: {len(command)} bytes")
            
            slave_addr = command[0]
            function_code = command[1]
            
            # Get or create PyModbus client
            client = await self.get_pymodbus_client(port, baudrate, loop)

            # Flush buffer if this is a new client's first command to this port
            client_id = command_info['client_id']
            port_key = (port, baudrate)
            if client_id not in self.client_port_initialized:
                self.client_port_initialized[client_id] = set()
            if port_key not in self.client_port_initialized[client_id]:
                self._flush_connected_serial_buffer(port, baudrate)
                self.client_port_initialized[client_id].add(port_key)

            # Enforce command spacing
            conn = self.pymodbus_connections[port][baudrate]
            time_since_last = time.time() - conn.last_used
            if time_since_last < conn.min_command_spacing:
                await asyncio.sleep(conn.min_command_spacing - time_since_last)

            # Execute based on function code
            if function_code == 0x01:  # Read Coils
                coil_address = struct.unpack('>H', command[2:4])[0]
                coil_count = struct.unpack('>H', command[4:6])[0]
                
                port_logger.info(
                    f"Read Coils: slave={hex(slave_addr)}, "
                    f"addr={hex(coil_address)}, count={coil_count}"
                )
                
                response = await asyncio.wait_for(
                    client.read_coils(
                        address=coil_address,
                        count=coil_count,
                        device_id=slave_addr
                    ),
                    timeout=command_info['timeout']
                )
                
                if response.isError():
                    raise Exception(f"Modbus error: {response}")
                
                # Convert bits to bytes
                response_bytes = self._coils_response_to_bytes(
                    slave_addr, function_code, response.bits
                )
                
            elif function_code == 0x02:  # Read Discrete Inputs
                input_address = struct.unpack('>H', command[2:4])[0]
                input_count = struct.unpack('>H', command[4:6])[0]
                
                port_logger.info(
                    f"Read Discrete Inputs: slave={hex(slave_addr)}, "
                    f"addr={hex(input_address)}, count={input_count}"
                )
                
                response = await asyncio.wait_for(
                    client.read_discrete_inputs(
                        address=input_address,
                        count=input_count,
                        device_id=slave_addr
                    ),
                    timeout=command_info['timeout']
                )
                
                if response.isError():
                    raise Exception(f"Modbus error: {response}")
                
                # Convert bits to bytes
                response_bytes = self._coils_response_to_bytes(
                    slave_addr, function_code, response.bits
                )
                
            elif function_code == 0x03:  # Read Holding Registers
                register_address = struct.unpack('>H', command[2:4])[0]
                register_count = struct.unpack('>H', command[4:6])[0]
                
                port_logger.info(
                    f"Read Holding: slave={hex(slave_addr)}, "
                    f"addr={hex(register_address)}, count={register_count}"
                )
                
                response = await asyncio.wait_for(
                    client.read_holding_registers(
                        address=register_address,
                        count=register_count,
                        device_id=slave_addr
                    ),
                    timeout=command_info['timeout']
                )
                
                if response.isError():
                    raise Exception(f"Modbus error: {response}")
                
                # Convert PyModbus response back to raw bytes for protocol compatibility
                response_bytes = self._modbus_response_to_bytes(
                    slave_addr, function_code, response.registers
                )
                
            elif function_code == 0x06:  # Write Single Register
                register_address = struct.unpack('>H', command[2:4])[0]
                register_value = struct.unpack('>H', command[4:6])[0]
                
                port_logger.info(
                    f"Write Register: slave={hex(slave_addr)}, "
                    f"addr={hex(register_address)}, value={register_value}"
                )
                
                response = await asyncio.wait_for(
                    client.write_register(
                        address=register_address,
                        value=register_value,
                        device_id=slave_addr
                    ),
                    timeout=command_info['timeout']
                )
                
                if response.isError():
                    raise Exception(f"Modbus error: {response}")
                
                # Echo back the write command as per Modbus spec
                response_bytes = command  # Write response echoes the request
                
            elif function_code == 0x05:  # Write Single Coil
                coil_address = struct.unpack('>H', command[2:4])[0]
                coil_value_raw = struct.unpack('>H', command[4:6])[0]
                coil_value = bool(coil_value_raw == 0xFF00)  # 0xFF00 = ON, 0x0000 = OFF
                
                port_logger.info(
                    f"Write Coil: slave={hex(slave_addr)}, "
                    f"addr={hex(coil_address)}, value={coil_value}"
                )
                
                response = await asyncio.wait_for(
                    client.write_coil(
                        address=coil_address,
                        value=coil_value,
                        device_id=slave_addr
                    ),
                    timeout=command_info['timeout']
                )
                
                if response.isError():
                    raise Exception(f"Modbus error: {response}")
                
                # Echo back the write command as per Modbus spec
                response_bytes = command
                
            elif function_code == 0x0F:  # Write Multiple Coils
                coil_address = struct.unpack('>H', command[2:4])[0]
                coil_count = struct.unpack('>H', command[4:6])[0]
                byte_count = command[6]
                
                # Extract coil values from bytes
                coil_values = []
                for byte_idx in range(byte_count):
                    byte_val = command[7 + byte_idx]
                    for bit_idx in range(8):
                        if len(coil_values) < coil_count:
                            coil_values.append(bool((byte_val >> bit_idx) & 1))
                
                port_logger.info(
                    f"Write Multiple Coils: slave={hex(slave_addr)}, "
                    f"addr={hex(coil_address)}, count={coil_count}"
                )
                
                response = await asyncio.wait_for(
                    client.write_coils(
                        address=coil_address,
                        values=coil_values,
                        device_id=slave_addr
                    ),
                    timeout=command_info['timeout']
                )
                
                if response.isError():
                    raise Exception(f"Modbus error: {response}")
                
                # Response format: slave + function + addr(2) + count(2) + crc(2)
                response_bytes = struct.pack(
                    '>BBHH',
                    slave_addr,
                    function_code,
                    coil_address,
                    coil_count
                )
                response_bytes += self._calculate_crc(response_bytes)
                
            elif function_code == 0x10:  # Write Multiple Registers
                register_address = struct.unpack('>H', command[2:4])[0]
                register_count = struct.unpack('>H', command[4:6])[0]
                byte_count = command[6]
                
                # Extract values
                values = []
                for i in range(register_count):
                    offset = 7 + (i * 2)
                    value = struct.unpack('>H', command[offset:offset+2])[0]
                    values.append(value)
                
                port_logger.info(
                    f"Write Multiple: slave={hex(slave_addr)}, "
                    f"addr={hex(register_address)}, count={register_count}, values={values}"
                )
                
                response = await asyncio.wait_for(
                    client.write_registers(
                        address=register_address,
                        values=values,
                        device_id=slave_addr
                    ),
                    timeout=command_info['timeout']
                )
                
                if response.isError():
                    raise Exception(f"Modbus error: {response}")
                
                # Response format: slave + function + addr(2) + count(2) + crc(2)
                response_bytes = struct.pack(
                    '>BBHH',
                    slave_addr,
                    function_code,
                    register_address,
                    register_count
                )
                response_bytes += self._calculate_crc(response_bytes)
                
            else:
                # Unsupported function - fall back to raw frame send
                port_logger.warning(f"Unsupported function code {hex(function_code)}, sending raw frame")
                # For unsupported functions, we can't use PyModbus - would need raw serial
                raise NotImplementedError(f"Function code {hex(function_code)} not yet supported by PyModbus mode")
            
            # Update last used time
            conn.last_used = time.time()
            
            return response_bytes
            
        except Exception as e:
            port_logger.error(f"PyModbus command failed: {str(e)}")
            # Flush buffer after error to resync framing for next command
            self._flush_connected_serial_buffer(port, baudrate)
            raise

    async def get_pymodbus_client(self, port: str, baudrate: int, loop: asyncio.AbstractEventLoop) -> AsyncModbusSerialClient:
        """Get or create PyModbus client for port/baudrate"""
        # Check if we have an existing client
        if baudrate in self.pymodbus_connections[port]:
            conn = self.pymodbus_connections[port][baudrate]
            if conn.client.connected:
                return conn.client
            else:
                # Client disconnected - MUST close before reconnecting to release serial port lock
                self.logger.info(f"PyModbus client for {port} disconnected, closing before reconnect...")
                try:
                    conn.client.close()
                except Exception as e:
                    self.logger.debug(f"Error closing disconnected client: {e}")
                del self.pymodbus_connections[port][baudrate]
                # Wait for serial port to be fully released by the OS
                await asyncio.sleep(0.1)
        
        # Create new PyModbus client
        self.logger.info(f"Creating PyModbus client for {port} @ {baudrate} baud")

        # Flush serial buffer BEFORE PyModbus connects (uses public pyserial API)
        try:
            import serial
            with serial.Serial(port, baudrate, timeout=0.1) as ser:
                ser.reset_input_buffer()
                self.logger.info(f"Flushed input buffer for {port}")
        except Exception as e:
            self.logger.warning(f"Could not pre-flush serial buffer for {port}: {e}")

        client = AsyncModbusSerialClient(
            port=port,
            baudrate=baudrate,
            bytesize=8,
            parity='N',
            stopbits=1,
            timeout=1.0,
            retries=0,  # No retries - prevents frame misalignment from late responses
            reconnect_delay=0.5,  # Auto-reconnect delay if connection drops
        )
        
        # Connect
        await client.connect()

        if not client.connected:
            raise Exception(f"Failed to connect PyModbus client to {port}")

        # Store connection with dynamic command spacing based on baud rate
        conn = PyModbusConnection(
            client=client,
            port=port,
            baudrate=baudrate,
            last_used=time.time(),
            event_loop=loop,
            min_command_spacing=calculate_min_command_spacing(baudrate)
        )
        self.logger.info(f"Command spacing for {port} @ {baudrate} baud: {conn.min_command_spacing*1000:.0f}ms")
        
        self.pymodbus_connections[port][baudrate] = conn
        self.logger.info(f"PyModbus client connected: {port} @ {baudrate} baud")
        
        return client

    async def _close_pymodbus_client(self, client: AsyncModbusSerialClient):
        """Close PyModbus client asynchronously"""
        try:
            client.close()
        except Exception as e:
            self.logger.error(f"Error closing PyModbus client: {e}")

    def _flush_connected_serial_buffer(self, port: str, baudrate: int) -> bool:
        """
        Flush serial input buffer on an active PyModbus connection.
        Uses PyModbus internals to access the underlying serial object.
        Returns True if flush succeeded.
        """
        try:
            if baudrate not in self.pymodbus_connections.get(port, {}):
                return False
            conn = self.pymodbus_connections[port][baudrate]
            if not conn.client.connected:
                return False
            # Access underlying serial through PyModbus transport
            if hasattr(conn.client, 'transport') and conn.client.transport:
                serial_obj = getattr(conn.client.transport, 'serial', None)
                if serial_obj and hasattr(serial_obj, 'reset_input_buffer'):
                    serial_obj.reset_input_buffer()
                    self.port_loggers[port].info(f"Flushed input buffer (active connection)")
                    return True
        except Exception as e:
            self.port_loggers[port].warning(f"Could not flush active connection buffer: {e}")
        return False

    def _modbus_response_to_bytes(self, slave_addr: int, function_code: int, registers: list) -> bytes:
        """
        Convert PyModbus response registers back to raw Modbus RTU bytes.
        
        Format: [slave_addr, function_code, byte_count, data..., crc_low, crc_high]
        """
        byte_count = len(registers) * 2
        response = struct.pack('BBB', slave_addr, function_code, byte_count)
        
        # Add register data
        for reg in registers:
            response += struct.pack('>H', reg)
        
        # Add CRC
        response += self._calculate_crc(response)
        
        return response

    def _coils_response_to_bytes(self, slave_addr: int, function_code: int, bits: list) -> bytes:
        """
        Convert PyModbus coil response (bits) back to raw Modbus RTU bytes.
        
        Format: [slave_addr, function_code, byte_count, coil_data..., crc_low, crc_high]
        Coils are packed 8 per byte, LSB first.
        """
        # Pack bits into bytes (8 bits per byte, LSB first)
        coil_bytes = []
        for i in range(0, len(bits), 8):
            byte_val = 0
            for bit_idx in range(8):
                if i + bit_idx < len(bits) and bits[i + bit_idx]:
                    byte_val |= (1 << bit_idx)
            coil_bytes.append(byte_val)
        
        byte_count = len(coil_bytes)
        response = struct.pack('BBB', slave_addr, function_code, byte_count)
        response += bytes(coil_bytes)
        
        # Add CRC
        response += self._calculate_crc(response)
        
        return response

    def _calculate_crc(self, data: bytes) -> bytes:
        """Calculate Modbus CRC16"""
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 1:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1
        # Return low byte first, then high byte
        return struct.pack('<H', crc)

    def send_response_sync(self, command_info: dict, response: bytes):
        """Send response back to client (same as original)"""
        try:
            client_id = command_info['client_id']
            command_id = command_info['command_id']
            
            if client_id not in self.clients:
                self.logger.debug(f"Client {client_id} disconnected, skipping response")
                return

            # Convert bytes to spaced hex format
            response_hex = ' '.join(f'{b:02X}' for b in response)
            timestamp = time.time()
            message = f"{command_info['command_id']}:{response_hex}:{timestamp:.4f}\n"
            
            try:
                command_info['socket'].send(message.encode())
                # Use shortened ID for logging readability
                command_parts = command_info['command_id'].split('_')
                short_command_id = '_'.join(command_parts[:-3])
                self.logger.debug(f"Response for {short_command_id}: {response_hex}")
            except (socket.error, IOError) as e:
                self.logger.debug(f"Socket error while sending response: {e}")
                return
            
            # Remove from pending using FULL command_id (must match what was added)
            if client_id in self.client_pending_commands:
                self.client_pending_commands[client_id].discard(command_info['command_id'])
        except Exception as e:
            self.logger.error(f"Failed to send response: {str(e)}")

    def send_error_sync(self, command_info: dict, error: str):
        """Send error message back to client (same as original)"""
        try:
            client_id = command_info['client_id']
            
            if client_id not in self.clients:
                self.logger.debug(f"Client {client_id} disconnected, skipping error message")
                return

            timestamp = time.time()
            message = f"{command_info['command_id']}:ERROR:{error}:{timestamp:.6f}\n"
            
            try:
                command_info['socket'].send(message.encode())
                self.logger.debug(f"Sent error to client {client_id}: {message.strip()}")
            except (socket.error, IOError) as e:
                self.logger.debug(f"Socket error while sending error: {e}")
                return
            
            # Remove from pending set (error is also a completed command)
            if client_id in self.client_pending_commands:
                self.client_pending_commands[client_id].discard(command_info['command_id'])
        except Exception as e:
            self.logger.error(f"Failed to send error: {str(e)}")

    def cleanup_client(self, client_id: int, client_socket):
        """Clean up resources for a disconnected client (same as original)"""
        self.logger.info(f"Cleaning up client {client_id}")

        if client_id in self.client_pending_commands:
            pending = self.client_pending_commands[client_id]
            if pending:
                self.logger.info(f"Cleaning up {len(pending)} pending commands for client {client_id}")
            self.client_pending_commands.pop(client_id, None)

        # Clean up port initialization tracking
        self.client_port_initialized.pop(client_id, None)
        
        if client_id in self.clients:
            self.clients.remove(client_id)
        
        try:
            client_socket.close()
        except Exception as e:
            self.logger.debug(f"Error during client {client_id} cleanup: {str(e)}")
        
        self.logger.info(f"Client disconnected: {client_id}")

    def stop(self):
        """Stop the server and cleanup resources"""
        self.logger.info("Shutting down PyModbus server...")
        self.shutdown_event.set()
        
        # Close all PyModbus connections
        for port in AVAILABLE_PORTS:
            for baudrate, conn in list(self.pymodbus_connections.get(port, {}).items()):
                try:
                    # Try async close if event loop is running
                    if conn.event_loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._close_pymodbus_client(conn.client),
                            conn.event_loop
                        )
                    else:
                        # Direct close if event loop not running
                        conn.client.close()
                except Exception as e:
                    self.logger.error(f"Error closing connection during shutdown: {e}")
                    # Last resort - try direct close
                    try:
                        conn.client.close()
                    except:
                        pass
        
        # Close server socket
        if hasattr(self, 'server_socket'):
            try:
                self.server_socket.close()
            except Exception as e:
                self.logger.error(f"Error closing server socket: {e}")
        
        self.logger.info("Server stopped")


if __name__ == "__main__":
    server = LuminaModbusServer()
    try:
        server.start()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.stop()
