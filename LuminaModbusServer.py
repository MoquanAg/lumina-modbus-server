"""
LuminaModbusServer: Hybrid implementation using asyncio for network and threading for serial.
"""

import asyncio
import concurrent.futures
from queue import Queue
import serial
from typing import Dict, Optional
import time
import logging
from dataclasses import dataclass
from LuminaLogger import LuminaLogger

AVAILABLE_PORTS = ['/dev/ttyAMA2', '/dev/ttyAMA3', '/dev/ttyAMA4']

@dataclass
class SerialConnection:
    port: serial.Serial
    last_used: float
    in_use: bool = False

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

    async def start(self):
        """Start the Modbus server and initialize all components."""
        try:
            # Start TCP server
            self.server = await asyncio.start_server(
                self.handle_client, 
                self.host, 
                self.port
            )
            self.logger.info(f"Server started on {self.host}:{self.port}")
            
            # Start serial processors in thread pool
            self.logger.info("Starting serial processors...")
            loop = asyncio.get_event_loop()
            for port in AVAILABLE_PORTS:
                self.logger.info(f"Starting processor for {port}")
                loop.run_in_executor(
                    self.thread_pool,
                    self.process_serial_port,
                    port
                )
            
            # Run server
            async with self.server:
                await self.server.serve_forever()
                
        except Exception as e:
            self.logger.error(f"Server startup failed: {str(e)}", exc_info=True)
            raise

    def process_serial_port(self, port: str):
        """Process commands for a specific serial port in a dedicated thread."""
        port_logger = self.port_loggers[port]
        port_logger.info(f"Serial processor started for {port}")
        
        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        last_command_time = 0
        
        while True:
            try:
                # Get command from queue (blocking)
                command_info = self.command_queues[port].get()
                port_logger.debug(f"RECEIVED COMMAND: {command_info['command_id']}")
                
                # Calculate and apply inter-message delay (3.5 char times is Modbus standard)
                current_time = time.time()
                baudrate = command_info['baudrate']
                char_time = 11 / baudrate  # 1 start + 8 data + 1 parity + 1 stop = 11 bits
                min_delay = 3.5 * char_time
                
                # Calculate how long to wait based on last command
                time_since_last = current_time - last_command_time
                if time_since_last < min_delay:
                    time.sleep(min_delay - time_since_last)
                
                try:
                    # Execute command
                    response = self.execute_serial_command(port, command_info)
                    last_command_time = time.time()  # Update last command time
                    
                    # Create and run coroutine in the event loop
                    async def send_response_coro():
                        await self.send_response(command_info, response)
                    
                    loop.run_until_complete(send_response_coro())
                    
                except Exception as e:
                    port_logger.error(f"Command execution failed: {str(e)}")
                    # Create and run coroutine in the event loop
                    async def send_error_coro():
                        await self.send_error(command_info, str(e))
                    
                    loop.run_until_complete(send_error_coro())
                    
            except Exception as e:
                port_logger.error(f"Error in serial processor: {str(e)}")
            finally:
                self.command_queues[port].task_done()

    def execute_serial_command(self, port: str, command_info: dict) -> bytes:
        """Execute a command on the serial port (runs in thread)."""
        port_logger = self.port_loggers[port]
        baudrate = command_info['baudrate']
        
        # Get or create serial connection
        serial_conn = self.get_serial_connection(port, baudrate)
        if not serial_conn:
            raise Exception(f"Could not establish serial connection on {port}")
        
        try:
            # Clear input buffer
            serial_conn.port.reset_input_buffer()
            
            # Write command
            command = command_info['command']
            port_logger.info(f"Writing {len(command)} bytes to {port}: {command.hex()}")
            serial_conn.port.write(command)
            
            # Calculate timing
            char_time = 11 / baudrate
            expected_length = command_info['response_length']
            timeout = max(char_time * expected_length * 1.5 + 0.05, 0.1)
            
            # Read response
            serial_conn.port.timeout = timeout
            response = serial_conn.port.read(expected_length)
            
            if len(response) != expected_length:
                raise Exception("Incomplete response")
                
            return response
            
        except Exception as e:
            port_logger.error(f"Serial command failed: {str(e)}")
            raise

    def get_serial_connection(self, port: str, baudrate: int) -> Optional[SerialConnection]:
        """Get or create a serial connection (runs in thread)."""
        try:
            # Create new connection
            ser = serial.Serial(
                port=port,
                baudrate=baudrate,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                bytesize=serial.EIGHTBITS,
                timeout=1
            )
            
            conn = SerialConnection(
                port=ser,
                last_used=time.time()
            )
            return conn
            
        except Exception as e:
            self.logger.error(f"Failed to get serial connection: {str(e)}", exc_info=True)
            return None

    async def send_response(self, command_info: dict, response: bytes):
        """Send response back to client."""
        try:
            response_hex = response.hex()
            # Add timestamp to the response format
            timestamp = time.time()
            message = f"{command_info['command_id']}:{response_hex}:{timestamp:.6f}\n"
            command_info['writer'].write(message.encode())
            await command_info['writer'].drain()
            # Add logging for successful response
            self.logger.debug(f"Sent response to client {command_info['client_id']}: {message.strip()}\n")
        except Exception as e:
            self.logger.error(f"Failed to send response: {str(e)}", exc_info=True)

    async def send_error(self, command_info: dict, error: str):
        """Send error message back to client."""
        try:
            # Add timestamp to the error response format
            timestamp = time.time()
            message = f"{command_info['command_id']}:ERROR:{error}:{timestamp:.6f}\n"
            command_info['writer'].write(message.encode())
            await command_info['writer'].drain()
            # Add logging for error response
            self.logger.debug(f"Sent error to client {command_info['client_id']}: {message.strip()}")
        except Exception as e:
            self.logger.error(f"Failed to send error: {str(e)}", exc_info=True)

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle incoming client connections and their messages."""
        client_id = id(writer)
        self.clients.add(client_id)
        self.logger.info(f"New client connected: {client_id}")
        
        try:
            while True:
                try:
                    data = await reader.readuntil(b'\n')
                    message = data.decode().strip()
                    await self.process_client_message(client_id, message, writer)
                except asyncio.IncompleteReadError:
                    self.logger.info(f"Client {client_id} disconnected")
                    break
                except Exception as e:
                    self.logger.error(f"Error handling client {client_id}: {str(e)}")
                    break
                    
        finally:
            self.clients.remove(client_id)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception as e:
                self.logger.debug(f"Error during client {client_id} cleanup: {str(e)}")
            self.logger.info(f"Client disconnected: {client_id}")

    async def process_client_message(self, client_id: int, message: str, writer: asyncio.StreamWriter):
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
                writer.write(error_response.encode())
                await writer.drain()
                return
            
            # Convert parameters
            try:
                command = bytes.fromhex(command_hex)
                response_length = int(response_length)
                baudrate = int(baudrate)
            except (ValueError, TypeError) as e:
                error_response = f"{command_id}:INVALID_PARAMETERS\n"
                writer.write(error_response.encode())
                await writer.drain()
                return
            
            # Create command info
            command_info = {
                'client_id': client_id,
                'command_id': command_id,
                'device_type': device_type,
                'command': command,
                'response_length': response_length,
                'timeout': timeout,
                'writer': writer,
                'baudrate': baudrate,
                'timestamp': time.time()
            }
            
            # Try to queue command
            try:
                # Convert the synchronous queue.put() to an async operation
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    self.command_queues[port].put,
                    command_info
                )
                
                self.logger.debug(
                    f"Queued command from {client_id}: "
                    f"ID={command_id}, Port={port}, "
                    f"Command={command_hex}, Length={response_length}"
                )
                
            except Exception as e:
                error_response = f"{command_id}:QUEUE_FULL\n"
                writer.write(error_response.encode())
                await writer.drain()
                
        except Exception as e:
            self.logger.error(f"Error processing message from client {client_id}: {str(e)}")

if __name__ == "__main__":
    server = LuminaModbusServer(max_queue_size=30, request_timeout=10)
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        server.logger.info("Server shutdown initiated")
