import asyncio
import logging
from asyncio import Queue
import serial_asyncio
import binascii
import uuid  # Add this import

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def format_bytes(data):
    """Format bytes as a readable hex string with ASCII representation."""
    hex_str = binascii.hexlify(data).decode('ascii')
    hex_pairs = ' '.join(hex_str[i:i+2] for i in range(0, len(hex_str), 2))
    ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)
    return f"{hex_pairs:<50}"

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
                self.serial_connections[port] = (reader, writer, baud_rate)
                self.command_queues[port] = asyncio.Queue()
                self.response_queues[port] = asyncio.Queue()
                asyncio.create_task(self.process_commands(port))
                logger.info(f"Serial connection established on port {port} with baud rate {baud_rate}")
            except Exception as e:
                logger.error(f"Failed to create serial connection on port {port}: {e}")
                raise

    async def process_commands(self, port):
        while True:
            try:
                if port not in self.serial_connections:
                    logger.info(f"Port {port} - Connection closed, exiting process_commands")
                    break

                reader, writer, _ = self.serial_connections[port]
                command, expected_length = await asyncio.wait_for(self.command_queues[port].get(), timeout=1)
                logger.info(f"Port {port} - Writing command: {format_bytes(command)}")
                writer.write(command)
                await writer.drain()
                
                response = b''
                discarded_bytes = b''
                start_bytes = command[:2]  # The first two bytes of the command should match the first two bytes of the response
                try:
                    # Read until we find a byte that matches either the first or second byte of start_bytes
                    while True:
                        byte = await asyncio.wait_for(reader.read(1), timeout=0.2)
                        if byte in [start_bytes[0:1], start_bytes[1:2]]:
                            response += byte
                            if len(response) == 1 and byte == start_bytes[1:2]:
                                # If we caught the second byte first, prepend with the expected first byte
                                response = start_bytes[0:1] + response
                            elif len(response) == 2 and response != start_bytes:
                                # If we have two bytes but they don't match start_bytes, keep only the second byte
                                discarded_bytes += response[0:1]
                                response = response[1:]
                            else:
                                break
                        else:
                            discarded_bytes += byte
                            logger.warning(f"Port {port} - Discarding unexpected byte: {format_bytes(byte)}")

                    # Now read the rest of the expected response
                    while len(response) < expected_length:
                        chunk = await asyncio.wait_for(reader.read(expected_length - len(response)), timeout=0.5)
                        if not chunk:  # No more data available
                            break
                        response += chunk
                    
                    logger.info(f"Port {port} - Received {len(response)} bytes: {format_bytes(response)}")
                    if discarded_bytes:
                        logger.info(f"Port {port} - Discarded {len(discarded_bytes)} bytes: {format_bytes(discarded_bytes)}")
                except asyncio.TimeoutError:
                    logger.warning(f"Port {port} - Timeout waiting for full response. Received {len(response)} bytes so far.")
                
                if len(response) != expected_length:
                    logger.warning(f"Port {port} - Received {len(response)} bytes, expected {expected_length}")
                
                # Include discarded bytes after start bytes in the response
                full_response = response + discarded_bytes[len(start_bytes):]
                
                # Always put the response in the queue, even if it's incomplete
                if port in self.response_queues:
                    await self.response_queues[port].put(full_response)
                else:
                    logger.warning(f"Port {port} - Response queue no longer exists, discarding response")

            except asyncio.CancelledError:
                logger.info(f"Port {port} - Command processing cancelled")
                break
            except asyncio.TimeoutError:
                logger.debug(f"Port {port} - Timeout waiting for command, continuing...")
            except Exception as e:
                logger.error(f"Port {port} - Error processing command: {e}")
                if port in self.response_queues:
                    await self.response_queues[port].put(b'')  # Put an empty response to unblock waiting coroutines
                await asyncio.sleep(1)  # Add a small delay to prevent tight looping on persistent errors
        
        logger.info(f"Port {port} - Exiting command processing loop")

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
        logger.info(f"Port {port} - Preparing {len(response)} bytes: {format_bytes(response)}")
        
        return response

    async def close_serial_connection(self, port):
        if port in self.serial_connections:
            _, writer, _ = self.serial_connections[port]
            writer.close()
            await writer.wait_closed()
            del self.serial_connections[port]
            del self.command_queues[port]
            del self.response_queues[port]
            logger.info(f"Closed serial connection on port {port}")

    async def handle_client(self, reader, writer):
        addr = writer.get_extra_info('peername')
        logger.info(f"New client connected: {addr}")
        
        last_activity = asyncio.get_event_loop().time()
        
        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.readuntil(b'\n'), timeout=600)  # 10 minutes timeout
                    if not data:
                        break
                    
                    last_activity = asyncio.get_event_loop().time()
                    
                    message = data.decode().strip()  # Strip any whitespace or newline characters
                    logger.info(f"Received message from {addr}: {message}")
                    
                    # Handle ping messages
                    if message == 'FF:F0:AA:AF':
                        logger.info(f"Received ping from {addr}")
                        await self.safe_write(writer, b'FF:F0:AA:AF\n')
                        logger.info(f"Sent pong to {addr}")
                        continue
                    
                    # Handle other commands
                    try:
                        command_uuid, name, port, baud_rate, command, response_length = message.split(':', 5)
                        baud_rate = int(baud_rate)
                        response_length = int(response_length)
                    except ValueError as e:
                        logger.error(f"Invalid message format from {addr}: {message}")
                        await self.safe_write(writer, b'ERROR: Invalid message format')
                        continue
                    
                    logger.info(f"{name} at {addr} - Received command: UUID={command_uuid}, port={port}, baud_rate={baud_rate}, command={command}, response_length={response_length}")
                    
                    # Check if we need to create a new serial connection or use an existing one
                    if port not in self.serial_connections or self.serial_connections[port][2] != baud_rate:
                        if port in self.serial_connections:
                            await self.close_serial_connection(port)
                        await self.create_serial_connection(port, baud_rate)
                    
                    command_bytes = bytes.fromhex(command)
                    response = await self.send_command(port, command_bytes, response_length)
                    
                    if response:
                        logger.info(f"To {name} at {addr} - Sending response for UUID {command_uuid}: {response.hex()}")
                        await self.safe_write(writer, f"{command_uuid}:{response.hex()}\n".encode())
                    else:
                        logger.warning(f"To {name} at {addr} - Sending empty response for UUID {command_uuid}")
                        await self.safe_write(writer, f"{command_uuid}:\n".encode())
                
                except asyncio.TimeoutError:
                    if asyncio.get_event_loop().time() - last_activity > 600:
                        logger.info(f"Client {addr} inactive for 10 minutes, closing connection")
                        break
                except ConnectionResetError:
                    logger.warning(f"Connection reset by client {addr}")
                    break
                except Exception as e:
                    logger.error(f"Error processing command from {addr}: {e}")
                    break
        
        except Exception as e:
            logger.error(f"Error handling client at {addr}: {e}")
        finally:
            logger.info(f"Client at {addr} disconnected")
            writer.close()
            try:
                await writer.wait_closed()
            except Exception as e:
                logger.warning(f"Error while closing writer for {addr}: {e}")
            # Close all serial connections associated with this client
            for port in list(self.serial_connections.keys()):
                await self.close_serial_connection(port)

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

    async def safe_write(self, writer, data):
        try:
            writer.write(data)
            await writer.drain()
        except ConnectionResetError:
            logger.warning("Connection reset by peer while writing")
        except BrokenPipeError:
            logger.warning("Broken pipe while writing")
        except Exception as e:
            logger.error(f"Error while writing: {e}")

# Run the server when the script is executed directly
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    server = LuminaModbusServer()
    asyncio.run(server.run())