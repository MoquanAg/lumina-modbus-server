import asyncio
import logging
from asyncio import Queue
import serial_asyncio
import binascii

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def format_bytes(data):
    """Format bytes as a readable hex string with ASCII representation."""
    hex_str = binascii.hexlify(data).decode('ascii')
    hex_pairs = ' '.join(hex_str[i:i+2] for i in range(0, len(hex_str), 2))
    ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)
    return f"{hex_pairs:<50} | {ascii_str}"

class LuminaModbusServer:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(LuminaModbusServer, cls).__new__(cls)
            cls._instance.initialize()
        return cls._instance

    def initialize(self):
        self.serial_connections = {}
        self.command_queues = {}
        self.response_queues = {}
        self.running = False

    async def create_serial_connection(self, port, baud_rate):
        if port not in self.serial_connections:
            try:
                reader, writer = await serial_asyncio.open_serial_connection(url=port, baudrate=baud_rate)
                self.serial_connections[port] = (reader, writer)
                self.command_queues[port] = asyncio.Queue()
                self.response_queues[port] = asyncio.Queue()
                asyncio.create_task(self.process_commands(port))
                logger.info(f"Serial connection established on port {port} with baud rate {baud_rate}")
            except Exception as e:
                logger.error(f"Failed to create serial connection on port {port}: {e}")
                raise

    async def process_commands(self, port):
        reader, writer = self.serial_connections[port]
        while True:
            try:
                command, expected_length = await self.command_queues[port].get()
                logger.info(f"Port {port} - Writing command: {format_bytes(command)}")
                writer.write(command)
                await writer.drain()
                
                response = b''
                try:
                    # Initial read with a longer timeout
                    response = await asyncio.wait_for(reader.read(expected_length), timeout=0.5)
                    
                    # If we got less than expected, try to read more
                    if len(response) < expected_length:
                        remaining = expected_length - len(response)
                        extra = await asyncio.wait_for(reader.read(remaining), timeout=0.5)
                        response += extra
                    
                    logger.info(f"Port {port} - Received response: {format_bytes(response)}")
                except asyncio.TimeoutError:
                    if response:
                        logger.warning(f"Port {port} - Partial response received before timeout: {format_bytes(response)}")
                    else:
                        logger.warning(f"Port {port} - Timeout waiting for response")
                
                await self.response_queues[port].put(response)
            except Exception as e:
                logger.error(f"Port {port} - Error processing command: {e}")

    async def send_command(self, port, command, expected_length):
        if port not in self.serial_connections:
            raise ValueError(f"Port {port} is not initialized")
        
        if isinstance(command, bytearray):
            command = bytes(command)
        
        logger.debug(f"Port {port} - Sending command: {format_bytes(command)}")
        await self.command_queues[port].put((command, expected_length))
        response = await self.response_queues[port].get()
        
        actual_length = len(response)
        if actual_length != expected_length:
            logger.warning(f"Port {port} - Received {actual_length} bytes, expected {expected_length}")
        logger.info(f"Port {port} - Preparing response: {format_bytes(response)}")
        return response

    async def handle_client(self, reader, writer):
        addr = writer.get_extra_info('peername')
        logger.info(f"New client connected: {addr}")
        
        try:
            while True:
                data = await reader.read(100)
                if not data:
                    break
                
                message = data.decode()
                port, baud_rate, command, response_length = message.split(':', 3)
                baud_rate = int(baud_rate)
                response_length = int(response_length)
                
                logger.debug(f"Client {addr} - Received command: port={port}, baud_rate={baud_rate}, command={command}, response_length={response_length}")
                
                if port not in self.serial_connections:
                    await self.create_serial_connection(port, baud_rate)
                else:
                    current_baud_rate = self.serial_connections[port][1].transport.serial.baudrate
                    if current_baud_rate != baud_rate:
                        logger.info(f"Port {port} - Updating baud rate from {current_baud_rate} to {baud_rate}")
                        old_reader, old_writer = self.serial_connections[port]
                        old_writer.close()
                        await old_writer.wait_closed()
                        await self.create_serial_connection(port, baud_rate)
                
                command_bytes = bytes.fromhex(command)
                response = await self.send_command(port, command_bytes, response_length)
                
                if response:
                    logger.debug(f"Client {addr} - Sending response: {response.hex()}")
                    writer.write(response.hex().encode())
                else:
                    logger.warning(f"Client {addr} - Sending empty response")
                    writer.write(b'')
                await writer.drain()
        except Exception as e:
            logger.error(f"Error handling client {addr}: {e}")
        finally:
            logger.info(f"Client disconnected: {addr}")
            writer.close()
            await writer.wait_closed()

    async def run(self):
        self.running = True
        server = await asyncio.start_server(
            self.handle_client, '127.0.0.1', 8888)

        addr = server.sockets[0].getsockname()
        logger.info(f'Server started. Listening on {addr}')

        async with server:
            await server.serve_forever()
            
    ### CRC16 Calculation
    # @staticmethod
    # def calculate_crc16(data: bytearray, high_byte_first: bool = True) -> bytearray:
    #     crc = 0xFFFF
    #     for byte in data:
    #         crc ^= byte
    #         for _ in range(8):
    #             if crc & 1:
    #                 crc = (crc >> 1) ^ 0xA001
    #             else:
    #                 crc >>= 1

    #     # Splitting the CRC into high and low bytes
    #     high_byte = crc & 0xFF
    #     low_byte = (crc >> 8) & 0xFF

    #     # Returning the CRC in the specified byte order
    #     if high_byte_first:
    #         return bytearray([high_byte, low_byte])
    #     else:
    #         return bytearray([low_byte, high_byte])

# Run the server when the script is executed directly
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    server = LuminaModbusServer()
    asyncio.run(server.run())