import asyncio
import serial_asyncio
from LuminaLogger import LuminaLogger

# logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# logger = logging.getLogger(__name__)


AVAILABLE_PORTS = ['/dev/ttyAMA2', '/dev/ttyAMA3', '/dev/ttyAMA4']

class LuminaModbusServer:
    def __init__(self, host='127.0.0.1', port=8888):
        self.host = host
        self.port = port
        self.clients = set()
        self.serial_ports = {}
        self.command_queues = {}
        self.recent_commands = {}
        self.server = None
        self.logger = LuminaLogger('LuminaModbusServer')
        self.port_loggers = {}
        for port_name in AVAILABLE_PORTS:
            self.port_loggers[port_name] = LuminaLogger(f'{port_name.split("/")[-1]}')

    async def start(self):
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
        try:
            if port_name in self.serial_ports and baudrate in self.serial_ports[port_name]:
                return self.serial_ports[port_name][baudrate]

            serial_port = await serial_asyncio.open_serial_connection(url=port_name, baudrate=baudrate)
            self.logger.info(f"Initialized serial port {port_name} with baudrate {baudrate}")
            self.serial_ports[port_name][baudrate] = serial_port
            return serial_port
        except Exception as e:
            self.logger.error(f"Failed to initialize serial port {port_name} with baudrate {baudrate}: {str(e)}")
            return None

    async def handle_client(self, reader, writer):
        client_id = id(writer)
        self.clients.add(client_id)
        self.logger.info(f"New client connected: {client_id}")
        try:
            while True:
                data = await reader.readuntil(b'\n')
                message = data.decode().strip()
                await self.process_client_message(client_id, message, writer)
        except asyncio.IncompleteReadError:
            pass
        finally:
            self.clients.remove(client_id)
            writer.close()
            await writer.wait_closed()
            self.logger.info(f"Client disconnected: {client_id}")

    async def process_client_message(self, client_id, message, writer):
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

        self.logger.info(f"Received from client {client_id}: Command ID: {command_id}, Port: {port}, Baud: {baudrate}, Command: {command_hex}")

        await self.command_queues[port].put({
            'client_id': client_id,
            'command_id': command_id,
            'command': command,
            'response_length': response_length,
            'timeout': timeout,
            'writer': writer,
            'baudrate': baudrate
        })

    async def process_command_queue(self, port):
        while True:
            command_info = await self.command_queues[port].get()
            try:
                await self.execute_modbus_command(port, command_info)
                await asyncio.sleep(0.05)
                
            except Exception as e:
                self.logger.error(f"Error processing command on port {port}: {str(e)}")
            finally:
                self.command_queues[port].task_done()

    async def execute_modbus_command(self, port, command_info):
        port_logger = self.port_loggers[port]
        baudrate = command_info['baudrate']
        if port not in self.serial_ports or baudrate not in self.serial_ports[port]:
            self.serial_ports[port][baudrate] = await self.init_serial_port(port, baudrate)
        
        reader, writer = self.serial_ports[port][baudrate]
        
        port_logger.info(f"Executing command on port {port}, baud rate {baudrate}")
        
        baud_rate = command_info['baudrate']
        char_time = 11 / baud_rate  # Time for one character (11 bits: 1 start + 8 data + 1 stop + 1 parity)
        
        # Calculate total expected transmission time for the response
        expected_response_length = command_info['response_length']
        transmission_time = char_time * expected_response_length
        # Add a safety margin of 50% to account for inter-character delays and processing time
        wait_time = transmission_time * 1.5

        start_time = asyncio.get_event_loop().time()
        
        port_logger.info(f"Sending {len(command_info['command'])} bytes to {port}: {' '.join(f'{b:02X}' for b in command_info['command'])}")
        port_logger.info(f"Expected response length: {expected_response_length} bytes")
        port_logger.info(f"Calculated wait time: {wait_time:.6f} seconds")
        
        writer.write(command_info['command'])
        await writer.drain()

        try:
            response = bytearray()
            remaining_length = expected_response_length
            timeout = command_info['timeout']

            await asyncio.sleep(wait_time)
            
            # Try to read the full response in one go
            chunk = await asyncio.wait_for(reader.read(remaining_length), timeout=timeout)
            response.extend(chunk)
            
            port_logger.info(f"Received {len(response)} bytes from {port}: {' '.join(f'{b:02X}' for b in response)}")
            
            # If we got all expected bytes, process the response and return
            if len(response) == expected_response_length:
                if len(response) >= 2 and response[:2] == command_info['command'][:2]:
                    hex_response = response.hex()
                    client_response = f"{command_info['command_id']}:{hex_response}\n"
                    command_info['writer'].write(client_response.encode())
                    await command_info['writer'].drain()
                    port_logger.info(f"Sent {len(response)} bytes to client {command_info['client_id']}")
                    end_time = asyncio.get_event_loop().time()
                    total_time = end_time - start_time
                    port_logger.info(f"Command completed successfully. Total time: {total_time:.6f} seconds")
                    return  # Exit the function on success
            
            # Only reach here if we didn't get enough bytes
            port_logger.warning(f"Incomplete response received ({len(response)}/{expected_response_length} bytes). Falling back to iterative reading.")
            remaining_length = expected_response_length - len(response)
            while remaining_length > 0 and timeout > 0:
                await asyncio.sleep(3.5 * char_time)
                read_start = asyncio.get_event_loop().time()
                chunk = await asyncio.wait_for(reader.read(remaining_length), timeout=timeout)
                read_end = asyncio.get_event_loop().time()
                
                chunk = chunk[:remaining_length]
                response.extend(chunk)
                remaining_length -= len(chunk)
                timeout -= (read_end - read_start)
                
                port_logger.info(f"Additional chunk received: {len(chunk)} bytes. Remaining: {remaining_length} bytes")

            if remaining_length > 0:
                raise asyncio.TimeoutError("Timed out while waiting for complete response")

            end_time = asyncio.get_event_loop().time()
            total_time = end_time - start_time
            port_logger.info(f"Total time from sending to receiving full response: {total_time:.6f} seconds")
            
            if len(response) >= 2 and response[:2] == command_info['command'][:2]:
                hex_response = response.hex()
                client_response = f"{command_info['command_id']}:{hex_response}\n"
                command_info['writer'].write(client_response.encode())
                await command_info['writer'].drain()
                port_logger.info(f"Sent {len(response)} bytes to client {command_info['client_id']}")
            
        except asyncio.TimeoutError:
            end_time = asyncio.get_event_loop().time()
            total_time = end_time - start_time
            port_logger.warning(f"Timeout waiting for response on port {port}. Total time: {total_time:.6f} seconds")

        await self.clear_buffer(reader)

    async def clear_buffer(self, reader):
        await asyncio.sleep(0.1)
        try:
            while True:
                chunk = await asyncio.wait_for(reader.read(100), timeout=0.1)
                if not chunk:
                    break
                self.logger.warning(f"Cleared {len(chunk)} extra bytes from buffer")
        except asyncio.TimeoutError:
            pass

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

        high_byte = crc & 0xFF
        low_byte = (crc >> 8) & 0xFF

        if high_byte_first:
            return bytearray([high_byte, low_byte])
        else:
            return bytearray([low_byte, high_byte])

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for port, (reader, writer) in self.serial_ports.items():
            writer.close()
            await writer.wait_closed()
        self.logger.info("Server stopped")

if __name__ == "__main__":
    server = LuminaModbusServer()
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        server.logger.info("Server shutdown initiated")
        asyncio.run(server.stop())
