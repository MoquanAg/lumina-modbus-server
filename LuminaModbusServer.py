"""
LuminaModbusServer: Hybrid implementation using asyncio for network and threading for serial.
"""

import concurrent.futures
from queue import Queue
import serial
from typing import Dict, Optional
import time
from dataclasses import dataclass
from LuminaLogger import LuminaLogger
import psutil
import os
import threading
import socket
import queue

AVAILABLE_PORTS = ['/dev/ttyAMA2', '/dev/ttyAMA3', '/dev/ttyAMA4']

@dataclass
class SerialConnection:
    port: serial.Serial
    last_used: float
    in_use: bool = False
    min_command_spacing: float = 0.05  # Increase to 50ms minimum between commands

class LuminaModbusServer:
    def __init__(self, host='127.0.0.1', port=8888, max_queue_size=100, request_timeout=30):
        # Server configuration
        self.host = host
        self.port = port
        self.request_timeout = request_timeout
        
        # Connection management
        self.clients = set()
        self.serial_ports: Dict[str, Dict[int, SerialConnection]] = {}
        self.command_queues = {
            port: Queue(maxsize=max_queue_size) 
            for port in AVAILABLE_PORTS
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
        
        # Add tracking of pending commands per client
        self.client_pending_commands = {}
        
        # Add serial connections dictionary to store persistent connections
        self.serial_connections = {
            port: {} for port in AVAILABLE_PORTS
        }
        
        # Add event to signal shutdown
        self.shutdown_event = threading.Event()

    def start(self):
        """Start the Modbus server and initialize all components."""
        try:
            # Start serial processors in thread pool
            self.logger.info("Starting serial processors...")
            
            # Create a list to track processor threads
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
            
            self.logger.info(f"Server started on {self.host}:{self.port}")
            
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
        """Handle incoming client connections and their messages."""
        client_id = id(client_socket)
        self.logger.info(f"New client connected: {client_id} from {address}")
        
        # Close any existing connections for all ports
        for port in AVAILABLE_PORTS:
            for baudrate, conn in list(self.serial_connections.get(port, {}).items()):
                try:
                    if conn.port.is_open:
                        conn.port.close()
                        self.logger.info(f"Closed existing serial connection on {port} at {baudrate} baud")
                except Exception as e:
                    self.logger.error(f"Error closing existing connection on {port}: {str(e)}")
            if port in self.serial_connections:
                self.serial_connections[port].clear()
        
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
                except ConnectionResetError:
                    self.logger.info(f"Client {client_id} connection reset")
                    break
                except ConnectionError as e:
                    self.logger.info(f"Client {client_id} connection error: {e}")
                    break
                except socket.error as e:
                    self.logger.info(f"Client {client_id} socket error: {e}")
                    break
                    
        except Exception as e:
            self.logger.error(f"Error handling client {client_id}: {str(e)}")
        finally:
            self.cleanup_client(client_id, client_socket)

    def process_serial_port(self, port: str):
        """Process commands for a specific serial port in a dedicated thread."""
        port_logger = self.port_loggers[port]
        port_logger.info(f"Serial processor started for {port}")
        
        while not self.shutdown_event.is_set():
            try:
                command_info = None  # Initialize outside try block
                try:
                    command_info = self.command_queues[port].get(timeout=1.0)
                except queue.Empty:
                    continue
                
                # Check if client is still connected before processing
                client_id = command_info['client_id']
                if client_id not in self.clients:
                    port_logger.debug(f"Skipping command for disconnected client {client_id}")
                    self.command_queues[port].task_done()  # Important: mark task as done
                    continue

                # Process command
                try:
                    response = self.execute_serial_command(port, command_info)
                    # Check again if client is still connected before sending response
                    if client_id in self.clients:
                        self.send_response_sync(command_info, response)
                except Exception as e:
                    port_logger.error(f"Error processing command: {str(e)}")
                    # Only try to send error if client is still connected
                    if client_id in self.clients:
                        self.send_error_sync(command_info, str(e))
                finally:
                    if command_info:  # Only mark as done if we actually got a command
                        try:
                            self.command_queues[port].task_done()
                        except ValueError:  # Handle "task_done() called too many times"
                            port_logger.debug("Task already marked as done")
                    
            except Exception as e:
                port_logger.error(f"Critical error in serial processor: {str(e)}")
                time.sleep(0.1)

    def send_response_sync(self, command_info: dict, response: bytes):
        """Send response back to client (synchronous version)."""
        try:
            client_id = command_info['client_id']
            command_id = command_info['command_id']
            # Check if client is still connected
            if client_id not in self.clients:
                self.logger.debug(f"Client {client_id} disconnected, skipping response")
                return

            # Convert bytes to spaced hex format
            response_hex = ' '.join(f'{b:02X}' for b in response)
            timestamp = time.time()
            message = f"{command_info['command_id']}:{response_hex}:{timestamp:.4f}\n"
            try:
                command_info['socket'].send(message.encode())
                # Modified logging format to show trimmed command ID
                command_parts = command_info['command_id'].split('_')
                command_id = '_'.join(command_parts[:-3])  # Join all parts except the last three
                self.logger.debug(f"Response for {command_id}: {response_hex}")
            except (socket.error, IOError) as e:
                self.logger.debug(f"Socket error while sending response to: {e}")
                return
            
            if client_id in self.client_pending_commands:
                self.client_pending_commands[client_id].discard(command_id)
        except Exception as e:
            self.logger.error(f"Failed to send response: {str(e)}")

    def send_error_sync(self, command_info: dict, error: str):
        """Send error message back to client (synchronous version)."""
        try:
            client_id = command_info['client_id']
            # Check if client is still connected
            if client_id not in self.clients:
                self.logger.debug(f"Client {client_id} disconnected, skipping error message")
                return

            timestamp = time.time()
            message = f"{command_info['command_id']}:ERROR:{error}:{timestamp:.6f}\n"
            try:
                command_info['socket'].send(message.encode())
                self.logger.debug(f"Sent error to client {client_id}: {message.strip()}")
            except (socket.error, IOError) as e:
                self.logger.debug(f"Socket error while sending error to client {client_id}: {e}")
        except Exception as e:
            self.logger.error(f"Failed to send error: {str(e)}")

    def cleanup_client(self, client_id: int, client_socket):
        """Clean up resources for a disconnected client."""
        self.logger.info(f"Client {client_id} disconnected")
        self.cleanup_client_commands(client_id)
        self.clients.remove(client_id)
        self.client_pending_commands.pop(client_id, None)
        try:
            client_socket.close()
        except Exception as e:
            self.logger.debug(f"Error during client {client_id} cleanup: {str(e)}")
        self.logger.info(f"Client disconnected: {client_id}")

    def execute_serial_command(self, port: str, command_info: dict) -> bytes:
        """Execute a command on the serial port (runs in thread)."""
        port_logger = self.port_loggers[port]
        baudrate = command_info['baudrate']
        
        # Validate response_length early
        expected_length = command_info.get('response_length')
        if not isinstance(expected_length, int) or expected_length <= 0:
            raise ValueError(f"Invalid response length: {expected_length}")
        
        # Add more detailed logging at entry point
        # port_logger.info(f"Starting serial execution for command {command_info['command_id']}")
        # port_logger.debug(f"Command details: Port={port}, Baudrate={baudrate}, Data={command_info['command'].hex()}")
        
        try:
            # Get or create serial connection
            serial_conn = self.get_serial_connection(port, baudrate)
            if not serial_conn:
                error_msg = f"Failed to get serial connection for command {command_info['command_id']}"
                port_logger.error(error_msg)
                raise Exception(error_msg)
            
            # Check if port is still open before proceeding
            if not serial_conn.port.is_open:
                error_msg = f"Serial port {port} was closed before command execution"
                port_logger.error(error_msg)
                raise Exception(error_msg)

            # port_logger.info(f"Successfully obtained serial connection for {port}")
            
            # Add a more substantial delay between commands
            time_since_last = time.time() - serial_conn.last_used
            if time_since_last < serial_conn.min_command_spacing:
                sleep_time = serial_conn.min_command_spacing - time_since_last
                # port_logger.debug(f"Sleeping {sleep_time:.3f}s between commands")
                time.sleep(sleep_time)
            
            # Clear any lingering data more aggressively
            serial_conn.port.reset_input_buffer()
            serial_conn.port.reset_output_buffer()
            
            # Update last_used time before command
            serial_conn.last_used = time.time()
            
            # Add serial port status check
            # port_logger.info(
            #     f"Serial port status before command: "
            #     f"CD={serial_conn.port.cd}, "
            #     f"CTS={serial_conn.port.cts}, "
            #     f"DSR={serial_conn.port.dsr}, "
            #     f"RI={serial_conn.port.ri}"
            # )
            
            # Log buffer clearing with buffer size
            in_waiting = serial_conn.port.in_waiting
            # port_logger.debug(f"Clearing input buffer (size={in_waiting}) for command {command_info['command_id']}")
            serial_conn.port.reset_input_buffer()
            
            # Modified logging format to show command ID but ignore last three underscore parts
            command = command_info['command']
            command_parts = command_info['command_id'].split('_')
            command_id = '_'.join(command_parts[:-3])  # Join all parts except the last three
            port_logger.info(
                f"Writing command {command_id}: "
                f"Length={len(command)} bytes, "
                f"Data={' '.join(f'{b:02X}' for b in command)}"
            )
            
            bytes_written = serial_conn.port.write(command)
            # port_logger.info(f"Successfully wrote {bytes_written} bytes for command {command_info['command_id']}")
            
            # Add immediate post-write buffer check
            # port_logger.debug(
            #     f"Post-write status: "
            #     f"out_waiting={serial_conn.port.out_waiting}, "
            #     f"in_waiting={serial_conn.port.in_waiting}"
            # )
            
            # Force write buffer flush
            serial_conn.port.flush()
            # port_logger.debug("Write buffer flushed")
            
            # Calculate timing (moved after validation)
            char_time = 11 / baudrate  # 1 start + 8 data + 1 parity + 1 stop = 11 bits
            base_timeout = char_time * expected_length * 1.5 + 0.05
            
            # Read response with timeout-based retry logic
            response = b''
            remaining_bytes = expected_length
            start_time = time.time()
            max_time = command_info['timeout']  # Use the command's timeout value
            
            while remaining_bytes > 0 and (time.time() - start_time) < max_time:
                # Calculate remaining time for this attempt
                time_elapsed = time.time() - start_time
                time_remaining = max_time - time_elapsed
                
                # Use the smaller of base_timeout or remaining time
                current_timeout = min(base_timeout, time_remaining)
                serial_conn.port.timeout = current_timeout
                
                # Log pre-read buffer status
                # port_logger.debug(
                #     f"Pre-read status (elapsed={time_elapsed:.3f}s): "
                #     f"in_waiting={serial_conn.port.in_waiting}, "
                #     f"timeout={current_timeout:.3f}s"
                # )
                
                chunk = serial_conn.port.read(remaining_bytes)
                response += chunk
                remaining_bytes = expected_length - len(response)
                
                if not chunk:  # If no bytes were read
                    port_logger.warning(
                        f"Partial read: received {len(response)}/{expected_length} bytes. "
                        f"Time elapsed: {time_elapsed:.3f}s/{max_time:.3f}s"
                    )
                    # Small delay before next attempt, scaled by baud rate
                    time.sleep(min(char_time * 5, time_remaining))
            
            if len(response) != expected_length:
                raise Exception(
                    f"Incomplete response after {time_elapsed:.3f}s. "
                    f"Received {len(response)} bytes, expected {expected_length} bytes"
                )
                
            return response
            
        except Exception as e:
            port_logger.error(f"Serial command failed: {str(e)}")
            # Ensure we're not trying to access a closed port
            if 'serial_conn' in locals() and serial_conn and serial_conn.port.is_open:
                try:
                    serial_conn.port.close()
                except Exception as close_error:
                    port_logger.error(f"Error closing port after failure: {str(close_error)}")
            raise

    def get_serial_connection(self, port: str, baudrate: int) -> Optional[SerialConnection]:
        """Get or create a serial connection (runs in thread)."""
        try:
            # Check if we already have a connection for this port/baudrate
            if port in self.serial_connections and baudrate in self.serial_connections[port]:
                conn = self.serial_connections[port][baudrate]
                if conn.port.is_open:
                    conn.last_used = time.time()
                    return conn
                else:
                    # If port was closed, remove it
                    del self.serial_connections[port][baudrate]

            # Create new connection
            ser = serial.Serial(
                port=port,
                baudrate=baudrate,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                bytesize=serial.EIGHTBITS,
                timeout=1,
                rtscts=True,  # Enable hardware flow control if supported
                dsrdtr=True   # Enable hardware flow control if supported
            )
            
            conn = SerialConnection(
                port=ser,
                last_used=time.time()
            )
            
            # Store the connection
            if port not in self.serial_connections:
                self.serial_connections[port] = {}
            self.serial_connections[port][baudrate] = conn
            
            return conn
            
        except Exception as e:
            self.logger.error(f"Failed to get serial connection: {str(e)}", exc_info=True)
            return None

    def cleanup_client_commands(self, client_id: int):
        """Clean up any pending commands for a disconnected client."""
        if client_id not in self.client_pending_commands:
            return
            
        pending_commands = self.client_pending_commands[client_id]
        self.logger.info(f"Cleaning up {len(pending_commands)} pending commands for client {client_id}")
        
        # Remove commands from all port queues
        for port in AVAILABLE_PORTS:
            # Create a new queue without the disconnected client's commands
            new_queue = Queue(maxsize=self.command_queues[port].maxsize)
            while not self.command_queues[port].empty():
                try:
                    cmd = self.command_queues[port].get_nowait()
                    if cmd['client_id'] != client_id:
                        new_queue.put(cmd)
                except Exception as e:
                    self.logger.error(f"Error during queue cleanup: {str(e)}")
            self.command_queues[port] = new_queue
            
            # Close and cleanup serial connections for this port
            if port in self.serial_connections:
                for baudrate, conn in list(self.serial_connections[port].items()):
                    try:
                        if conn.port.is_open:
                            conn.port.close()
                            self.logger.info(f"Closed serial connection on {port} at {baudrate} baud")
                    except Exception as e:
                        self.logger.error(f"Error closing serial connection on {port}: {str(e)}")
                self.serial_connections[port].clear()

    def process_client_message(self, client_id: int, message: str, client_socket):
        """Process incoming messages from clients and queue commands."""
        parts = message.split(':')
        if len(parts) < 6:
            self.logger.warning(f"Invalid message format from client {client_id}: {message}")
            return

        try:
            # Parse message parts
            command_id, device_type, port, baudrate, command_hex, response_length, *rest = parts
            timeout = float(rest[0]) if rest else 1.0
            
            # Validate port
            if port not in AVAILABLE_PORTS:
                error_response = f"{command_id}:INVALID_PORT\n"
                client_socket.send(error_response.encode())
                return
            
            # Convert parameters
            try:
                command = bytes.fromhex(command_hex)
                response_length = int(response_length)
                baudrate = int(baudrate)
            except (ValueError, TypeError) as e:
                error_response = f"{command_id}:INVALID_PARAMETERS\n"
                client_socket.send(error_response.encode())
                return
            
            # Create command info
            command_info = {
                'client_id': client_id,
                'command_id': command_id,
                'device_type': device_type,
                'command': command,
                'response_length': response_length,
                'timeout': timeout,
                'socket': client_socket,
                'baudrate': baudrate,
                'timestamp': time.time()
            }
            
            # Add command ID to pending commands before queuing
            self.client_pending_commands[client_id].add(command_id)
            
            # Try to queue command
            try:
                self.command_queues[port].put(command_info)
                
                # self.logger.debug(
                #     f"Queued command from {client_id}: "
                #     f"ID={command_id}, Port={port}, "
                #     f"Command={command_hex}, Length={response_length}"
                # )
                
            except Queue.Full:
                # Remove command ID if queuing failed
                self.client_pending_commands[client_id].remove(command_id)
                error_response = f"{command_id}:QUEUE_FULL\n"
                client_socket.send(error_response.encode())
                
        except Exception as e:
            self.logger.error(f"Error processing message from client {client_id}: {str(e)}")

if __name__ == "__main__":
    # Kill any existing instances
    # current_pid = os.getpid()
    # current_process = psutil.Process(current_pid)
    # current_name = current_process.name()
    
    # for proc in psutil.process_iter(['pid', 'name']):
    #     try:
    #         # If it's the same program name but not our current process
    #         if proc.info['name'] == current_name and proc.pid != current_pid:
    #             proc.kill()
    #             print(f"Killed existing process: {proc.pid}")
    #     except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
    #         pass

    server = LuminaModbusServer(max_queue_size=30, request_timeout=10)
    try:
        server.start()  # Remove asyncio.run() since start() is now synchronous
    except KeyboardInterrupt:
        server.logger.info("Server shutdown initiated")
        server.shutdown_event.set()
