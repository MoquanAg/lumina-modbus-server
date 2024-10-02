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
                # Add this line:
                asyncio.create_task(self.process_commands(port))
                logging.info(f"Serial connection established on port {port} with baud rate {baud_rate}")
            except Exception as e:
                logging.error(f"Failed to create serial connection on port {port}: {str(e)}")
                raise

    async def process_commands(self, port):
        reader, writer = self.serial_connections[port]
        while True:
            try:
                command = await self.command_queues[port].get()
                logger.info(f"Writing command to port {port}:\n{format_bytes(command)}")
                writer.write(command)
                await writer.drain()
                logger.info(f"Command written to port {port}, waiting for response...")
                
                # Add a timeout for reading the response
                try:
                    start_time = asyncio.get_event_loop().time()
                    response = await asyncio.wait_for(reader.read(100), timeout=0.5)  # Increased buffer size and timeout
                    end_time = asyncio.get_event_loop().time()
                    logger.info(f"Received response from port {port} in {end_time - start_time:.5f} seconds:\n{format_bytes(response)}")
                    await self.response_queues[port].put(response)
                except asyncio.TimeoutError:
                    end_time = asyncio.get_event_loop().time()
                    logger.warning(f"Timeout after {end_time - start_time:.5f} seconds waiting for response from port {port}")
                    await self.response_queues[port].put(b'')  # Put an empty response in the queue
            except Exception as e:
                logger.error(f"Error processing command on port {port}: {str(e)}")

    async def send_command(self, port, command, response_length):
        if port not in self.serial_connections:
            raise ValueError(f"Port {port} is not initialized")
        
        # Convert bytearray to bytes if necessary
        if isinstance(command, bytearray):
            command = bytes(command)
        
        # Calculate CRC16 and append it to the command
        crc = self.calculate_crc16(bytearray(command), high_byte_first=True)
        command_with_crc = command + crc

        logger.info(f"Sending command to port {port}:\n{format_bytes(command_with_crc)}")
        await self.command_queues[port].put(command_with_crc)
        response = await self.response_queues[port].get()
        if response:
            logger.info(f"Preparing to send response to client from port {port}:\n{format_bytes(response[:response_length])}")
        else:
            logger.warning(f"No response received from port {port}")
        return response[:response_length]

    async def handle_client(self, reader, writer):
        addr = writer.get_extra_info('peername')
        logger.info(f"New client connected: {addr}")
        
        while True:
            data = await reader.read(100)
            if not data:
                break
            
            message = data.decode()
            port, baud_rate, command, response_length = message.split(':', 3)
            baud_rate = int(baud_rate)
            response_length = int(response_length)
            
            logger.info(f"Received command from {addr}: port={port}, baud_rate={baud_rate}, command={command}, response_length={response_length}")
            
            if port not in self.serial_connections:
                await self.create_serial_connection(port, baud_rate)
            elif self.serial_connections[port][1].baudrate != baud_rate:
                logger.info(f"Updating baud rate for port {port} from {self.serial_connections[port][1].baudrate} to {baud_rate}")
                # Close existing connection and create a new one with updated baud rate
                old_reader, old_writer = self.serial_connections[port]
                old_writer.close()
                await old_writer.wait_closed()
                await self.create_serial_connection(port, baud_rate)
            
            response = await self.send_command(port, bytes.fromhex(command), response_length)
            
            if response:
                logger.info(f"Sending response to {addr}: {response.hex()}")
                writer.write(response.hex().encode())
                await writer.drain()
            else:
                logger.warning(f"Sending empty response to {addr}")
                writer.write(b'')
                await writer.drain()

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
    @staticmethod
    def calculate_crc16(data: bytearray, high_byte_first: bool = True) -> bytearray:
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

# Run the server when the script is executed directly
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    server = LuminaModbusServer()
    asyncio.run(server.run())