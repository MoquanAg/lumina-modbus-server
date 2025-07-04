"""
LuminaModbusClient: Asynchronous Modbus client implementation for industrial communication.
Handles command queuing, response processing, and connection management with automatic reconnection.

Features:
- Asynchronous command processing
- Automatic reconnection
- CRC16 calculation
- Command timeout handling
- Observer pattern for response handling
"""

import asyncio
import logging
import uuid  # Add this import
import time  # Add this import
import random
import string
# import GLOBALS
# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# logger = GLOBALS.logger

class LuminaModbusClient:
    """
    Asynchronous Modbus client for industrial communication with automatic reconnection and command queuing.
    
    Attributes:
        response_queues (dict): Stores response queues for commands
        read_lock (asyncio.Lock): Lock for thread-safe reading operations
        observers (list): List of observers for response handling
        recent_commands (dict): Cache of recently sent commands
        response_timeout (int): Timeout duration for responses in seconds
        command_queue (asyncio.Queue): Queue for pending commands
        command_timeout (int): Timeout duration for individual commands
    """

    def __init__(self):
        self.initialize()
        self.response_queues = {}
        self.read_lock = asyncio.Lock()
        self.observers = []  
        self.recent_commands = {}  # New attribute to store recent commands
        self.response_timeout = 5  # 5 seconds timeout for responses
        self.command_queue = asyncio.Queue()
        self.processing_task = None
        self._read_lock = asyncio.Lock()
        self.command_timeout = 5  # 5 seconds timeout for individual commands

    def initialize(self):
        """
        Initialize core client attributes and connection state.
        Sets up connection-related attributes and timing parameters.
        """
        self.reader = None
        self.writer = None
        self.communication_lock = asyncio.Lock()
        self.keep_alive_task = None
        self.is_connected = False
        self.last_ping_time = 0
        self.ping_interval = 30  # Set the ping interval to 2 seconds

    async def connect(self, host='127.0.0.1', port=8888):
        """
        Establish connection to the Modbus server.

        Args:
            host (str): Server hostname or IP address
            port (int): Server port number

        Raises:
            Exception: If connection fails
        """
        try:
            self.reader, self.writer = await asyncio.open_connection(host, port)
            self.is_connected = True
            logger.info(f"Connected to server at {host}:{port}")
            # self._start_keep_alive()
            asyncio.create_task(self._cleanup_recent_commands())  # Start the cleanup task
            # Start the response reading task
            asyncio.create_task(self._read_responses())
        except Exception as e:
            logger.info(f"Failed to connect: {str(e)}")
            self.is_connected = False
            raise

    def _start_keep_alive(self):
        if self.keep_alive_task is None or self.keep_alive_task.done():
            self.keep_alive_task = asyncio.create_task(self._keep_alive())

    async def _keep_alive(self):
        """
        Maintain connection with periodic ping messages.
        Handles reconnection if ping fails.
        """
        while self.is_connected:
            try:
                current_time = asyncio.get_event_loop().time()
                if current_time - self.last_ping_time >= self.ping_interval:
                    async with self.communication_lock:
                        if self.writer and not self.writer.is_closing():
                            ping_message = b'FF:F0:AA:AF\n'  # New ping command
                            self.writer.write(ping_message)
                            await self.writer.drain()
                            logger.info(f"Sent ping: {ping_message.strip().decode()}")
                            
                            # Wait for pong response
                            try:
                                pong_response = await asyncio.wait_for(self.reader.readuntil(b'\n'), timeout=1.0)
                                if pong_response.strip() == b'FF:F0:AA:AF':
                                    logger.info("Received pong response")
                                else:
                                    logger.warning(f"Unexpected response to ping: {pong_response}")
                            except asyncio.TimeoutError:
                                logger.warning("Timeout waiting for pong response")
                            
                            self.last_ping_time = current_time
                        else:
                            logger.info("Writer is not available or is closing. Attempting to reconnect...")
                            await self._reconnect()
            except Exception as e:
                logger.info(f"Error in keep-alive: {str(e)}. Attempting to reconnect...")
                await self._reconnect()
            await asyncio.sleep(0.1)  # Short sleep to prevent busy-waiting

    async def _reconnect(self):
        """
        Attempt to reconnect to the server after connection loss.
        Implements exponential backoff retry mechanism.
        """
        self.is_connected = False
        await self.close()  # Ensure existing connection is closed
        while not self.is_connected:
            try:
                await self.connect()
                logger.info("Successfully reconnected to server")
                break
            except Exception as e:
                logger.info(f"Failed to reconnect: {str(e)}. Retrying in 5 seconds...")
                await asyncio.sleep(5)

    async def close(self):
        """
        Close the client connection and cleanup resources.
        Cancels keep-alive task and closes writer/reader streams.
        """
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception as e:
                logger.info(f"Error while closing connection: {str(e)}")
        self.reader = None
        self.writer = None
        self.is_connected = False
        if self.keep_alive_task:
            self.keep_alive_task.cancel()
            try:
                await self.keep_alive_task
            except asyncio.CancelledError:
                pass
        self.keep_alive_task = None

    async def send_command(self, name, port, command, baudrate=9600, response_length=20, host='127.0.0.1', server_port=8888, timeout=None):
        """
        Queue a command for sending to the Modbus server.

        Args:
            name (str): Command identifier
            port (str): Serial port path
            command (bytes): Command data
            baudrate (int): Serial communication baudrate
            response_length (int): Expected response length
            host (str): Server hostname
            server_port (int): Server port
            timeout (int, optional): Command-specific timeout

        Returns:
            str: Unique command identifier
        """
        # Truncate the hex command if it's longer than 12 characters
        truncated_hex = command.hex()[:12] if len(command.hex()) > 12 else command.hex()
        # Generate random 2-character alphanumeric string
        random_suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=2))
        port_name = port.split("/")[-1]
        send_time = time.strftime('%Y%m%d%H%M%S')
        command_id = f"{port_name}_{name}_{truncated_hex}_{send_time}_{random_suffix}"
        command_info = {
            'name': name,
            'port': port,
            'command': command,
            'baudrate': baudrate,
            'response_length': response_length,
            'host': host,
            'server_port': server_port,
            'id': command_id,
            'timeout': timeout or self.command_timeout
        }
        
        logger.debug(f"Queueing command: {command_info}")
        await self.command_queue.put(command_info)
        
        if self.processing_task is None or self.processing_task.done():
            self.processing_task = asyncio.create_task(self._process_command_queue())

        return command_id

    async def _process_command_queue(self):
        """
        Process queued commands asynchronously.
        Handles command execution and error handling for the command queue.
        """
        while not self.command_queue.empty():
            command_info = await self.command_queue.get()
            try:
                await self._send_command(command_info)
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Error processing command: {e}")
            finally:
                self.command_queue.task_done()

    async def _send_command(self, command_info):
        """
        Send a single command to the server with CRC calculation.

        Args:
            command_info (dict): Command information including ID, name, port, etc.

        Raises:
            Exception: If command sending fails
        """
        async with self.communication_lock:
            try:
                if not self.is_connected:
                    await self.connect(command_info['host'], command_info['server_port'])
                
                crc = self.calculate_crc16(command_info['command'], high_byte_first=True)
                command_with_crc = command_info['command'] + crc

                message_parts = [
                    command_info['id'],
                    command_info['name'],
                    command_info['port'],
                    str(command_info['baudrate']),
                    command_with_crc.hex(),
                    str(command_info['response_length'])
                ]

                if command_info['timeout'] is not None:
                    message_parts.append(str(command_info['timeout']))

                message = ":".join(message_parts) + "\n"
                
                logger.debug(f"Sending command: {message.strip()}")
                
                self.writer.write(message.encode())
                await self.writer.drain()

                self.recent_commands[command_info['id']] = {
                    'command': command_with_crc.hex(),
                    'response_length': command_info['response_length'],
                    'timestamp': time.time(),
                    'timeout': command_info['timeout']
                }

            except Exception as e:
                logger.error(f"Error during command execution: {str(e)}", exc_info=True)
                await self._reconnect()
                raise

    async def _read_responses(self):
        """
        Continuously read and process responses from the server.
        Handles response parsing, timeout detection, and observer notifications.
        """
        while True:
            try:
                async with self._read_lock:
                    response = await asyncio.wait_for(self.reader.readuntil(b'\n'), timeout=self.response_timeout)
                response = response.strip()
                
                logger.debug(f"Received raw response: {response}")
                
                response_parts = response.decode().split(':', 1)
                response_uuid = response_parts[0]
                response_data = response_parts[1] if len(response_parts) > 1 else ''
                
                if response_uuid in self.recent_commands:
                    command_info = self.recent_commands[response_uuid]
                    if response_data in ['MISMATCH', 'TIMEOUT']:
                        logger.warning(f"Received {response_data} for command {response_uuid}")
                        await self.notify_observers({"type": "error", "data": {"command_uuid": response_uuid, "error": response_data}})
                    elif not response_data or len(bytes.fromhex(response_data)) == command_info['response_length']:
                        logger.debug(f"Received matching response for command {response_uuid}")
                        await self.notify_observers({"type": "response", "data": response})
                    else:
                        logger.warning(f"Response length mismatch for command {response_uuid}")
                        await self.notify_observers({"type": "error", "data": {"command_uuid": response_uuid, "error": "Response length mismatch"}})
                    del self.recent_commands[response_uuid]
                else:
                    logger.warning(f"Received unmatched response: {response}")
                    await self.notify_observers({"type": "unmatched_response", "data": response})
            
            except ValueError as e:
                logger.error(f"Error parsing response: {str(e)}")
                await self.notify_observers({"type": "error", "data": {"error": "Invalid response format"}})
            
            except asyncio.TimeoutError:
                logger.debug("Timeout while waiting for response, checking for timed out commands")
                current_time = time.time()
                timed_out_commands = [uuid for uuid, info in self.recent_commands.items() 
                                      if current_time - info['timestamp'] > info['timeout']]
                
                for uuid in timed_out_commands:
                    logger.warning(f"Timeout for command: {uuid}")
                    await self.notify_observers({"type": "timeout", "data": {"command_uuid": uuid}})
                    del self.recent_commands[uuid]
            
            except asyncio.IncompleteReadError as e:
                logger.error(f"IncompleteReadError: {e.partial} bytes read on a total of {e.expected} expected bytes")
                await self._reconnect()

            except Exception as e:
                logger.error(f"Error in _read_responses: {str(e)}", exc_info=True)
                await asyncio.sleep(1)  # Add a small delay before retrying

    # Add a new method to clean up old commands
    async def _cleanup_recent_commands(self):
        """
        Periodically clean up expired commands from recent_commands cache.
        Removes commands older than 10 seconds.
        """
        while True:
            current_time = time.time()
            self.recent_commands = {
                uuid: info for uuid, info in self.recent_commands.items()
                if current_time - info['timestamp'] <= 10
            }
            await asyncio.sleep(1)  # Run cleanup every second

    async def notify_observers(self, message):
        for observer in self.observers:
            if asyncio.iscoroutinefunction(observer.process_received_message):
                await observer.process_received_message(message)
            else:
                observer.process_received_message(message)

    ### CRC16 Calculation
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

    def add_observer(self, observer):
        """
        Add an observer to receive response notifications.

        Args:
            observer: Observer object implementing process_received_message
        """
        if observer not in self.observers:
            self.observers.append(observer)

    def remove_observer(self, observer):
        """
        Remove an observer from the notification list.

        Args:
            observer: Observer object to remove
        """
        if observer in self.observers:
            self.observers.remove(observer)

