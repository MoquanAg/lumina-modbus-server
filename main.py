"""
LuminaModbusServer: Raw pyserial implementation for reliable Modbus RTU communication.

This version uses raw pyserial for direct serial I/O with true timeout control,
replacing PyModbus which had issues with hanging on unresponsive sensors.

Key features:
- Direct pyserial control with native timeouts
- No asyncio complexity - simple synchronous I/O
- Manual CRC validation
- Same TCP protocol - no client changes needed!
"""

import concurrent.futures
from queue import Queue
from typing import Dict, Optional
import time
from dataclasses import dataclass
from LuminaLogger import LuminaLogger
import psutil
import os
import threading
import socket
import queue
import struct
import serial

AVAILABLE_PORTS = ['/dev/ttyAMA0', '/dev/ttyAMA1', '/dev/ttyAMA2', '/dev/ttyAMA3', '/dev/ttyAMA4']

@dataclass
class SerialConnection:
    """Serial port connection wrapper for direct pyserial I/O"""
    serial_port: serial.Serial
    port: str
    baudrate: int
    last_used: float
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

        # Serial connection pool (one connection per port/baudrate combination)
        self.serial_connections: Dict[str, Dict[int, SerialConnection]] = {
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

        self.logger.info("Raw pyserial Lumina Modbus Server initialized")

    def start(self):
        """Start the Modbus server and initialize all components."""
        try:
            # Start serial processors in thread pool
            self.logger.info("Starting serial processors with raw pyserial...")
            
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
            
            self.logger.info(f"Server started on {self.host}:{self.port} (raw pyserial mode)")
            
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
        
        # Serial connections are shared across all clients - no need to close them
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

            # Log received command
            command_hex = ' '.join(f'{b:02X}' for b in command_bytes)
            self.logger.info(f"Client {client_id} -> {port}: {command_hex}")

            self.command_queues[port].put(command_info, timeout=1.0)
            self.client_pending_commands[client_id].add(command_id)
            
        except queue.Full:
            self.logger.error(f"Command queue full for {port}")
            self.send_error_sync(command_info, "queue_full")
        except Exception as e:
            self.logger.error(f"Error processing client message: {str(e)}")

    def process_serial_port(self, port: str):
        """Process commands for a specific serial port using raw pyserial."""
        port_logger = self.port_loggers[port]
        port_logger.info(f"Serial processor started for {port}")

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

                # Process command using raw pyserial
                try:
                    response = self.execute_serial_command(port, command_info)

                    if client_id in self.clients:
                        self.send_response_sync(command_info, response)
                except Exception as e:
                    port_logger.error(f"Serial command failed: {str(e)}")
                    if client_id in self.clients:
                        self.send_error_sync(command_info, str(e))
                finally:
                    if command_info:
                        try:
                            self.command_queues[port].task_done()
                        except ValueError:
                            port_logger.debug("Task already marked as done")

            except Exception as e:
                port_logger.error(f"Critical error in serial processor: {str(e)}")
                time.sleep(0.1)

    def execute_serial_command(self, port: str, command_info: dict) -> bytes:
        """
        Execute Modbus command using raw pyserial with reliable timeout.

        The command already includes CRC from the client. We just:
        1. Flush input buffer
        2. Write command
        3. Read response with timeout
        4. Validate CRC
        5. Return raw response
        """
        port_logger = self.port_loggers[port]
        baudrate = command_info['baudrate']
        command = command_info['command']
        timeout = command_info['timeout']
        response_length = command_info['response_length']

        # Parse for logging
        if len(command) >= 2:
            slave_addr = command[0]
            function_code = command[1]
            port_logger.info(f"Command: slave={hex(slave_addr)}, func={hex(function_code)}, len={len(command)}")

        # Get or create serial connection
        conn = self.get_serial_connection(port, baudrate, timeout)

        # Enforce command spacing
        time_since_last = time.time() - conn.last_used
        if time_since_last < conn.min_command_spacing:
            time.sleep(conn.min_command_spacing - time_since_last)

        # Flush any stale data from both buffers
        conn.serial_port.reset_input_buffer()
        conn.serial_port.reset_output_buffer()

        # Write command
        conn.serial_port.write(command)
        conn.serial_port.flush()  # Ensure data is sent

        # Wait for TX to physically complete before reading echo
        # At 9600 baud: 8 bytes ≈ 8.3ms, add margin for safety
        tx_time = (len(command) * 10) / baudrate  # 10 bits per byte (start + 8 data + stop)
        time.sleep(tx_time + 0.005)  # TX time + 5ms margin

        # RS-485 half-duplex: discard TX echo before reading response
        # Many RS-485 transceivers echo transmitted data back on RX
        command_len = len(command)
        echo = conn.serial_port.read(command_len)
        if echo:
            if echo == command:
                port_logger.debug(f"Discarded TX echo ({len(echo)} bytes)")
            else:
                # Echo doesn't match - might be noise, late response, or partial echo
                # Just log and clear buffer - don't drain with timeout as we might
                # accidentally drain the actual response that's about to arrive
                echo_hex = ' '.join(f'{b:02X}' for b in echo)
                port_logger.warning(f"Discarded unexpected data ({len(echo)} bytes): {echo_hex}")
                # Quick non-blocking clear of anything currently in buffer
                stale = conn.serial_port.read(conn.serial_port.in_waiting)
                if stale:
                    stale_hex = ' '.join(f'{b:02X}' for b in stale)
                    port_logger.warning(f"Cleared buffer ({len(stale)} bytes): {stale_hex}")

        # Read response with retry loop for partial reads
        # (Critical: responses may arrive in chunks, especially at low baud rates)
        response = b''
        remaining_bytes = response_length
        start_time = time.time()
        max_time = timeout

        while remaining_bytes > 0 and (time.time() - start_time) < max_time:
            # Calculate remaining time for this read attempt
            time_elapsed = time.time() - start_time
            time_remaining = max_time - time_elapsed

            if time_remaining <= 0:
                break

            # Set timeout to remaining time (but cap at original timeout)
            conn.serial_port.timeout = min(time_remaining, timeout)

            chunk = conn.serial_port.read(remaining_bytes)
            if chunk:
                response += chunk
                remaining_bytes = response_length - len(response)
            else:
                # No data received - log and continue waiting
                if len(response) > 0:
                    port_logger.debug(
                        f"Partial read: {len(response)}/{response_length} bytes, "
                        f"waiting {time_remaining:.2f}s more"
                    )

        # Restore original timeout
        conn.serial_port.timeout = timeout

        # Update last used time
        conn.last_used = time.time()

        # Check if we got enough data
        if len(response) < response_length:
            elapsed = time.time() - start_time
            # Drain any remaining bytes that might arrive late
            self._drain_serial_buffer(conn, port_logger)
            # Don't close port - reconnections cause more issues than they solve
            # Just drain buffer and let next command retry on same connection
            raise Exception(
                f"Incomplete response after {elapsed:.2f}s: "
                f"{len(response)}/{response_length} bytes"
            )

        # Validate CRC
        data = response[:-2]
        received_crc = response[-2:]
        expected_crc = self._calculate_crc(data)
        if received_crc != expected_crc:
            response_hex = ' '.join(f'{b:02X}' for b in response)
            port_logger.error(
                f"CRC mismatch: got {received_crc.hex()}, expected {expected_crc.hex()}, "
                f"response ({len(response)} bytes): {response_hex}"
            )
            # Drain any remaining stale bytes to prevent contaminating next read
            self._drain_serial_buffer(conn, port_logger)
            # Don't close port - keep connection open for retry
            raise Exception(f"CRC mismatch in response")

        port_logger.info(f"Response: {len(response)} bytes, CRC valid")
        return response

    def _drain_serial_buffer(self, conn: SerialConnection, port_logger):
        """
        Quickly drain any remaining bytes from serial buffer after an error.
        Uses a short timeout and max drain time to avoid blocking.
        """
        try:
            # Temporarily set very short timeout for draining
            old_timeout = conn.serial_port.timeout
            conn.serial_port.timeout = 0.02  # 20ms - just enough to catch late bytes

            # Read whatever is in the buffer right now (non-blocking-ish)
            drained = conn.serial_port.read(1024)  # Read up to 1KB

            # Restore timeout immediately
            conn.serial_port.timeout = old_timeout

            if drained:
                drained_hex = ' '.join(f'{b:02X}' for b in drained)
                port_logger.info(f"Drained {len(drained)} stale bytes: {drained_hex}")
        except Exception as e:
            port_logger.warning(f"Error draining buffer: {e}")
            # Restore timeout on error
            try:
                conn.serial_port.timeout = old_timeout
            except:
                pass

    def _close_serial_connection(self, port: str, baudrate: int, port_logger):
        """
        Close serial connection and remove from pool after errors.
        Forces a fresh reconnect on next command, which resets UART state.
        """
        try:
            if baudrate in self.serial_connections.get(port, {}):
                conn = self.serial_connections[port][baudrate]
                if conn.serial_port.is_open:
                    conn.serial_port.close()
                    port_logger.info(f"Closed serial port {port} @ {baudrate} for recovery")
                del self.serial_connections[port][baudrate]
        except Exception as e:
            port_logger.warning(f"Error closing serial connection: {e}")

    def get_serial_connection(self, port: str, baudrate: int, timeout: float) -> SerialConnection:
        """Get or create serial connection for port/baudrate."""
        # Check if we have an existing connection
        if baudrate in self.serial_connections[port]:
            conn = self.serial_connections[port][baudrate]
            # Update timeout if different
            if conn.serial_port.timeout != timeout:
                conn.serial_port.timeout = timeout
            # Check if port is still open
            if conn.serial_port.is_open:
                return conn
            else:
                # Connection closed - remove it
                self.logger.info(f"Serial connection for {port} closed, reconnecting...")
                try:
                    conn.serial_port.close()
                except Exception:
                    pass
                del self.serial_connections[port][baudrate]

        # Create new serial connection
        self.logger.info(f"Creating serial connection for {port} @ {baudrate} baud")

        ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=8,
            parity='N',
            stopbits=1,
            timeout=timeout,
            # Disabled hardware flow control - Pi UART pins often not connected
            # which can cause handshaking issues and initial command failures
            rtscts=False,
            dsrdtr=False
        )

        # Flush any stale data and let the bus settle
        # Longer settle time helps RS-485 transceiver stabilize
        ser.reset_input_buffer()
        time.sleep(0.1)  # 100ms settle time
        ser.reset_input_buffer()  # Flush again in case noise arrived
        self.logger.info(f"Flushed input buffer for {port}")

        # Store connection with dynamic command spacing based on baud rate
        conn = SerialConnection(
            serial_port=ser,
            port=port,
            baudrate=baudrate,
            last_used=time.time(),
            min_command_spacing=calculate_min_command_spacing(baudrate)
        )
        self.logger.info(f"Command spacing for {port} @ {baudrate} baud: {conn.min_command_spacing*1000:.0f}ms")

        self.serial_connections[port][baudrate] = conn
        self.logger.info(f"Serial connection opened: {port} @ {baudrate} baud")

        return conn

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
        self.logger.info("Shutting down server...")
        self.shutdown_event.set()

        # Close all serial connections
        for port in AVAILABLE_PORTS:
            for baudrate, conn in list(self.serial_connections.get(port, {}).items()):
                try:
                    conn.serial_port.close()
                    self.logger.info(f"Closed serial connection: {port} @ {baudrate}")
                except Exception as e:
                    self.logger.error(f"Error closing serial connection: {e}")

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
