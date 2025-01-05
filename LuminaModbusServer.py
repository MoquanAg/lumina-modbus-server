"""
LuminaModbusServer: Asynchronous Modbus server implementation for handling multiple serial ports.
Manages client connections, command queuing, and serial communication with proper timing.
"""

import asyncio
import serial_asyncio
from typing import Dict, Optional
import time
import logging
from dataclasses import dataclass
from LuminaLogger import LuminaLogger

AVAILABLE_PORTS = ['/dev/ttyAMA2', '/dev/ttyAMA3', '/dev/ttyAMA4']

@dataclass
class SerialConnection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
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
            port: asyncio.Queue(maxsize=max_queue_size) 
            for port in AVAILABLE_PORTS
        }
        
        # Port locks for thread safety
        self.port_locks = {
            port: asyncio.Lock() 
            for port in AVAILABLE_PORTS
        }
        
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
            
            # Initialize command processors for each port
            processors = [
                self.process_command_queue(port) 
                for port in AVAILABLE_PORTS
            ]
            
            # Start maintenance tasks
            maintenance = [
                self.monitor_serial_connections(),
                self.cleanup_old_connections()
            ]
            
            # Combine all tasks
            tasks = processors + maintenance
            
            # Run server and all tasks
            async with self.server:
                await asyncio.gather(
                    self.server.serve_forever(),
                    *tasks
                )
                
        except Exception as e:
            self.logger.error(f"Server startup failed: {str(e)}")
            raise

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
                await asyncio.wait_for(
                    self.command_queues[port].put(command_info),
                    timeout=0.1
                )
                self.logger.debug(
                    f"Queued command from {client_id}: "
                    f"ID={command_id}, Port={port}, "
                    f"Command={command_hex}, Length={response_length}"
                )
                
            except (asyncio.TimeoutError, asyncio.QueueFull):
                error_response = f"{command_id}:QUEUE_FULL\n"
                writer.write(error_response.encode())
                await writer.drain()
                
        except Exception as e:
            self.logger.error(f"Error processing message from client {client_id}: {str(e)}")

    async def process_command_queue(self, port: str):
        """Process commands in the queue for a specific port."""
        port_logger = self.port_loggers[port]
        
        while True:
            try:
                # Get next command
                command_info = await self.command_queues[port].get()
                
                # Check if command is too old
                age = time.time() - command_info['timestamp']
                if age > self.request_timeout:
                    port_logger.warning(
                        f"Dropping old request (age: {age:.1f}s) "
                        f"from client {command_info['client_id']}"
                    )
                    error_response = f"{command_info['command_id']}:REQUEST_EXPIRED\n"
                    command_info['writer'].write(error_response.encode())
                    await command_info['writer'].drain()
                    continue
                
                # Execute command
                async with self.port_locks[port]:
                    await self.execute_modbus_command(port, command_info)
                    
            except Exception as e:
                port_logger.error(f"Error processing command on {port}: {str(e)}")
            finally:
                self.command_queues[port].task_done()

    async def execute_modbus_command(self, port: str, command_info: dict):
        """Execute a Modbus command on specified port with timing calculations."""
        port_logger = self.port_loggers[port]
        baudrate = command_info['baudrate']
        
        try:
            # Get or create serial connection
            serial_conn = await self.get_serial_connection(port, baudrate)
            if not serial_conn:
                raise Exception(f"Could not establish serial connection on {port}")
            
            # Calculate timing
            char_time = 11 / baudrate  # Time for one character (11 bits per char)
            expected_length = command_info['response_length']
            transmission_time = char_time * expected_length
            wait_time = transmission_time * 1.5 + 0.02
            
            # Clear any leftover data
            await self.clear_buffer(serial_conn.reader)
            
            # Convert hex string to bytes if needed
            command = command_info['command']
            if isinstance(command, str):
                command = bytes.fromhex(command)
            
            # Send command
            port_logger.debug(
                f"Sending {len(command)} bytes: "
                f"{' '.join(f'{b:02X}' for b in command)}"
            )
            serial_conn.writer.write(command)
            await serial_conn.writer.drain()
            
            # Wait for response
            try:
                response = await asyncio.wait_for(
                    self.read_response(serial_conn.reader, expected_length),
                    timeout=wait_time
                )
                
                # Send response back to client
                response_hex = response.hex()
                client_response = f"{command_info['command_id']}:{response_hex}\n"
                command_info['writer'].write(client_response.encode())
                await command_info['writer'].drain()
                
                port_logger.debug(
                    f"Command completed successfully: "
                    f"Response={' '.join(f'{b:02X}' for b in response)}"
                )
                
            except asyncio.TimeoutError:
                error_response = f"{command_info['command_id']}:TIMEOUT\n"
                command_info['writer'].write(error_response.encode())
                await command_info['writer'].drain()
                
        except Exception as e:
            error_response = f"{command_info['command_id']}:ERROR\n"
            command_info['writer'].write(error_response.encode())
            await command_info['writer'].drain()
            port_logger.error(f"Command execution failed: {str(e)}")
            
        finally:
            # Always clear buffer after command
            if 'serial_conn' in locals():
                await self.clear_buffer(serial_conn.reader)

    async def get_serial_connection(self, port: str, baudrate: int) -> Optional[SerialConnection]:
        """Get or create a serial connection for the specified port and baudrate."""
        try:
            if port not in self.serial_ports:
                self.serial_ports[port] = {}
                
            if baudrate not in self.serial_ports[port]:
                reader, writer = await serial_asyncio.open_serial_connection(
                    url=port,
                    baudrate=baudrate
                )
                self.serial_ports[port][baudrate] = SerialConnection(
                    reader=reader,
                    writer=writer,
                    last_used=time.time()
                )
                
            conn = self.serial_ports[port][baudrate]
            conn.last_used = time.time()
            return conn
            
        except Exception as e:
            self.logger.error(f"Failed to get serial connection {port}@{baudrate}: {str(e)}")
            return None

    async def clear_buffer(self, reader: asyncio.StreamReader, timeout: float = 0.1):
        """Clear any remaining data in the serial port buffer."""
        try:
            while True:
                data = await asyncio.wait_for(reader.read(100), timeout=timeout)
                if not data:
                    break
        except asyncio.TimeoutError:
            pass

    async def read_response(self, reader: asyncio.StreamReader, length: int) -> bytes:
        """Read exact number of bytes from serial port."""
        response = bytearray()
        while len(response) < length:
            chunk = await reader.read(length - len(response))
            if not chunk:
                raise ConnectionError("Serial connection broken")
            response.extend(chunk)
        return bytes(response)

    async def monitor_serial_connections(self):
        """Monitor serial connections for health and usage."""
        while True:
            try:
                for port, baudrate_conns in self.serial_ports.items():
                    for baudrate, conn in baudrate_conns.items():
                        if time.time() - conn.last_used > 300:  # 5 minutes
                            self.logger.info(
                                f"Closing inactive connection: {port}@{baudrate}"
                            )
                            conn.writer.close()
                            await conn.writer.wait_closed()
                            del baudrate_conns[baudrate]
            except Exception as e:
                self.logger.error(f"Error in connection monitor: {str(e)}")
            await asyncio.sleep(60)  # Check every minute

    async def cleanup_old_connections(self):
        """Clean up expired connections and commands."""
        while True:
            try:
                for port in AVAILABLE_PORTS:
                    queue = self.command_queues[port]
                    new_queue = asyncio.Queue(maxsize=queue.maxsize)
                    
                    while not queue.empty():
                        command = queue.get_nowait()
                        age = time.time() - command['timestamp']
                        if age <= self.request_timeout:
                            await new_queue.put(command)
                    
                    self.command_queues[port] = new_queue
                    
            except Exception as e:
                self.logger.error(f"Error in connection cleanup: {str(e)}")
            await asyncio.sleep(30)  # Run every 30 seconds

if __name__ == "__main__":
    server = LuminaModbusServer(max_queue_size=30, request_timeout=10)
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        server.logger.info("Server shutdown initiated")
