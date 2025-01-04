"""
LuminaModbusServer: Asynchronous Modbus server implementation for handling multiple serial ports.
Manages client connections, command queuing, and serial communication with proper timing.

Features:
- Multi-client support
- Multiple serial port handling
- Baudrate-specific connections
- Proper timing calculations for Modbus RTU
- Comprehensive logging
"""

import asyncio
import serial_asyncio
from LuminaLogger import LuminaLogger

# logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# logger = logging.getLogger(__name__)


AVAILABLE_PORTS = ['/dev/ttyAMA2', '/dev/ttyAMA3', '/dev/ttyAMA4']

class LuminaModbusServer:
    """
    Asynchronous Modbus server implementation for handling multiple serial ports.
    Manages client connections, command queuing, and serial communication with proper timing.

    Attributes:
        host (str): Server host address
        port (int): Server port number
        clients (set): Set of connected client IDs
        serial_ports (dict): Dictionary of serial port connections by port name and baudrate
        command_queues (dict): Command queues for each port
        recent_commands (dict): Cache of recently executed commands
        server (asyncio.Server): Asyncio server instance
        logger (LuminaLogger): Main server logger
        port_loggers (dict): Individual loggers for each port
        request_timeout (float): Maximum age of requests in seconds
    """

    def __init__(self, host='127.0.0.1', port=8888, max_queue_size=100, request_timeout=30):
        """
        Initialize the Modbus server with host and port configuration.

        Args:
            host (str): Server host address, defaults to localhost
            port (int): Server port number, defaults to 8888
            max_queue_size (int): Maximum size of the command queue
            request_timeout (float): Maximum age of requests in seconds
        """
        self.host = host
        self.port = port
        self.clients = set()
        self.serial_ports = {}
        self.command_queues = {}
        self.recent_commands = {}
        self.server = None
        self.logger = LuminaLogger('LuminaModbusServer')
        self.port_loggers = {}
        self.request_timeout = request_timeout  # Maximum age of requests in seconds
        for port_name in AVAILABLE_PORTS:
            self.port_loggers[port_name] = LuminaLogger(f'{port_name.split("/")[-1]}')
            self.command_queues[port_name] = asyncio.Queue(maxsize=max_queue_size)

    async def start(self):
        """
        Start the Modbus server and initialize all serial ports.
        Creates command processing tasks for each available port.
        """
        self.server = await asyncio.start_server(
            self.handle_client, self.host, self.port)
        self.logger.info(f"Server started on {self.host}:{self.port}")
        
        # Initialize serial ports
        for port_name in AVAILABLE_PORTS:
            self.serial_ports[port_name] = {}
            self.command_queues[port_name] = asyncio.Queue()
            asyncio.create_task(self.process_command_queue(port_name))

        async with self.server:
            await self.server.serve_forever()

    async def init_serial_port(self, port_name, baudrate):
        """
        Initialize a serial port with specified baudrate.

        Args:
            port_name (str): Serial port device path
            baudrate (int): Communication baudrate

        Returns:
            tuple: (reader, writer) pair for serial communication, or None if initialization fails
        """
        try:
            if port_name in self.serial_ports and baudrate in self.serial_ports[port_name]:
                return self.serial_ports[port_name][baudrate]

            serial_port = await serial_asyncio.open_serial_connection(url=port_name, baudrate=baudrate)
            self.logger.info(f"Initialized serial port {port_name} with baudrate {baudrate}")
            
            # Wait a moment for the port to stabilize
            await asyncio.sleep(0.1)
            
            # Clear any potential leftover data
            reader, writer = serial_port
            await self.clear_buffer(reader)
            self.logger.info(f"Cleared initial buffer for port {port_name}")
            
            self.serial_ports[port_name][baudrate] = serial_port
            return serial_port
        except Exception as e:
            self.logger.error(f"Failed to initialize serial port {port_name} with baudrate {baudrate}: {str(e)}")
            return None

    async def handle_client(self, reader, writer):
        """
        Handle incoming client connections and their messages.

        Args:
            reader (StreamReader): Async stream reader for client
            writer (StreamWriter): Async stream writer for client
        """
        client_id = id(writer)
        self.clients.add(client_id)
        self.logger.info(f"New client connected: {client_id}")
        try:
            while True:
                try:
                    data = await reader.readuntil(b'\n')
                    message = data.decode().strip()
                    await self.process_client_message(client_id, message, writer)
                except (ConnectionResetError, ConnectionError) as e:
                    self.logger.info(f"Client {client_id} connection reset: {str(e)}")
                    break
                except asyncio.IncompleteReadError:
                    self.logger.info(f"Client {client_id} disconnected")
                    break
        except Exception as e:
            self.logger.error(f"Error handling client {client_id}: {str(e)}")
        finally:
            self.clients.remove(client_id)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception as e:
                self.logger.debug(f"Error during client {client_id} cleanup: {str(e)}")
            self.logger.info(f"Client disconnected: {client_id}")

    async def process_client_message(self, client_id, message, writer):
        """
        Process incoming messages from clients and queue commands.

        Args:
            client_id (int): Unique client identifier
            message (str): Received message string
            writer (StreamWriter): Client's stream writer for responses
        """
        parts = message.split(':')
        if len(parts) < 6:
            self.logger.warning(f"Invalid message format from client {client_id}: {message}")
            return

        command_id, name, port, baudrate, command_hex, response_length, *rest = parts
        timeout = float(rest[0]) if rest else 1.0  # Default timeout of 5 seconds

        try:
            command = bytes.fromhex(command_hex)
            response_length = int(response_length)
            baudrate = int(baudrate)
        except ValueError as e:
            self.logger.error(f"Invalid data in message from client {client_id}: {str(e)}")
            return

        self.logger.info(f"Received from client {client_id}: Command ID: {command_id}, Port: {port}, Baud: {baudrate}, Command: {command_hex}, Response Length: {response_length}, Timeout: {timeout}")

        command_info = {
            'client_id': client_id,
            'command_id': command_id,
            'command': command,
            'response_length': response_length,
            'timeout': timeout,
            'writer': writer,
            'baudrate': baudrate,
            'timestamp': asyncio.get_event_loop().time()  # Add timestamp
        }

        try:
            # Will raise QueueFull if queue is at max_queue_size
            await asyncio.wait_for(
                self.command_queues[port].put(command_info),
                timeout=0.1  # Short timeout for queue insertion
            )
        except (asyncio.TimeoutError, asyncio.QueueFull):
            error_response = f"{command_id}:QUEUE_FULL\n"
            writer.write(error_response.encode())
            await writer.drain()

    async def process_command_queue(self, port):
        """
        Process commands in the queue for a specific port.

        Args:
            port (str): Serial port identifier
        """
        while True:
            # Check and clean old requests before processing next item
            await self.clean_old_requests(port)
            
            command_info = await self.command_queues[port].get()
            try:
                # Check if request is too old before processing
                age = asyncio.get_event_loop().time() - command_info['timestamp']
                if age > self.request_timeout:
                    self.logger.warning(f"Dropping old request (age: {age:.1f}s) from client {command_info['client_id']}")
                    error_response = f"{command_info['command_id']}:REQUEST_EXPIRED\n"
                    command_info['writer'].write(error_response.encode())
                    await command_info['writer'].drain()
                else:
                    await self.execute_modbus_command(port, command_info)
                
            except Exception as e:
                self.logger.error(f"Error processing command on port {port}: {str(e)}")
            finally:
                self.command_queues[port].task_done()

    async def clean_old_requests(self, port):
        """Clean out expired requests from the queue without client notification."""
        current_time = asyncio.get_event_loop().time()
        
        # Create a new queue
        new_queue = asyncio.Queue(maxsize=self.command_queues[port].maxsize)
        
        # Move items to new queue, silently dropping expired ones
        while not self.command_queues[port].empty():
            try:
                item = self.command_queues[port].get_nowait()
                age = current_time - item['timestamp']
                
                if age <= self.request_timeout:
                    await new_queue.put(item)
                else:
                    self.logger.warning(f"Dropped expired request from client {item['client_id']} (age: {age:.1f}s)")
            except asyncio.QueueEmpty:
                break

        # Replace old queue with new one
        self.command_queues[port] = new_queue

    async def execute_modbus_command(self, port, command_info):
        """
        Execute a Modbus command on specified port with timing calculations.

        Args:
            port (str): Serial port identifier
            command_info (dict): Command details including baudrate, timeout, and expected response
        """
        port_logger = self.port_loggers[port]
        baudrate = command_info['baudrate']
        command_id = command_info['command_id']
        
        if port not in self.serial_ports or baudrate not in self.serial_ports[port]:
            self.serial_ports[port][baudrate] = await self.init_serial_port(port, baudrate)
        
        reader, writer = self.serial_ports[port][baudrate]
        
        port_logger.info(f"Executing command on port {port}, baud rate {baudrate}")
        
        baud_rate = command_info['baudrate']
        char_time = 11 / baud_rate  # Time for one character
        
        expected_response_length = command_info['response_length']
        transmission_time = char_time * expected_response_length
        wait_time = transmission_time * 1.5 + 0.02

        start_time = asyncio.get_event_loop().time()
        
        # Clear buffer before sending new command
        await self.clear_buffer(reader)
        
        port_logger.info(f"Sending {len(command_info['command'])} bytes to {port}: {' '.join(f'{b:02X}' for b in command_info['command'])}")
        port_logger.info(f"Expected response length: {expected_response_length} bytes")
        port_logger.info(f"Calculated wait time: {wait_time:.6f} seconds")
        
        try:
            # Set a strict overall timeout for the entire command execution
            async with asyncio.timeout(command_info['timeout']):
                writer.write(command_info['command'])
                await writer.drain()
                await asyncio.sleep(wait_time)
                
                response = bytearray()
                remaining_length = expected_response_length

                while remaining_length > 0:
                    chunk = await reader.read(remaining_length)
                    if not chunk:
                        break
                    
                    response.extend(chunk)
                    remaining_length -= len(chunk)
                    
                    if remaining_length > 0:
                        # Shorter delay between reads
                        await asyncio.sleep(char_time)
                        port_logger.debug(f"Partial response received: {len(response)}/{expected_response_length} bytes")

                if len(response) > 0:
                    port_logger.info(f"Received {len(response)} bytes from {port}: {' '.join(f'{b:02X}' for b in response)}")
                
                if len(response) == expected_response_length:
                    if len(response) >= 2 and response[:2] == command_info['command'][:2]:
                        # Just use the response as-is, it already includes CRC from the sensor
                        hex_response = response.hex()
                        client_response = f"{command_info['command_id']}:{hex_response}\n"
                        command_info['writer'].write(client_response.encode())
                        await command_info['writer'].drain()
                        port_logger.debug(f"Sent {len(response)} bytes to client {command_info['client_id']}")
                        end_time = asyncio.get_event_loop().time()
                        total_time = end_time - start_time
                        port_logger.info(f"Command completed successfully. Total time: {total_time:.6f} seconds")
                        return
                
                # Improved error handling for incomplete responses
                if len(response) != expected_response_length:
                    raise asyncio.TimeoutError(f"Incomplete response: got {len(response)}/{expected_response_length} bytes")
                
        except (asyncio.TimeoutError, Exception) as e:
            end_time = asyncio.get_event_loop().time()
            total_time = end_time - start_time
            port_logger.warning(f"Error on port {port} for command ID {command_id}. {str(e)}. Total time: {total_time:.6f} seconds")
            # Immediately send error response to client to prevent blocking
            error_response = f"{command_info['command_id']}:ERROR\n"
            command_info['writer'].write(error_response.encode())
            await command_info['writer'].drain()
        finally:
            # Aggressive buffer clearing after any error
            await self.clear_buffer(reader, aggressive=True)

    async def clear_buffer(self, reader, aggressive=False):
        """
        Clear any remaining data in the serial port buffer.

        Args:
            reader (StreamReader): Serial port reader
            aggressive (bool): If True, uses a shorter timeout and multiple attempts for aggressive clearing
        """
        timeout = 0.05 if aggressive else 0.1  # Shorter timeout for aggressive clearing
        max_attempts = 3 if aggressive else 1   # Multiple clearing attempts if aggressive

        for attempt in range(max_attempts):
            try:
                while True:
                    chunk = await asyncio.wait_for(reader.read(100), timeout=timeout)
                    if not chunk:
                        break
                    self.logger.warning(f"Cleared {len(chunk)} extra bytes from buffer (attempt {attempt + 1})")
            except asyncio.TimeoutError:
                break

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

        high_byte = crc & 0xFF
        low_byte = (crc >> 8) & 0xFF

        if high_byte_first:
            return bytearray([high_byte, low_byte])
        else:
            return bytearray([low_byte, high_byte])

    async def stop(self):
        """
        Stop the server and close all connections.
        Closes server and all serial port connections gracefully.
        """
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for port, (reader, writer) in self.serial_ports.items():
            writer.close()
            await writer.wait_closed()
        self.logger.info("Server stopped")

if __name__ == "__main__":
    server = LuminaModbusServer(max_queue_size=30, request_timeout=10)  # 30-second timeout
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        server.logger.info("Server shutdown initiated")
        asyncio.run(server.stop())
