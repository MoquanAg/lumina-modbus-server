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
        baudrate = command_info['baudrate']
        if port not in self.serial_ports or baudrate not in self.serial_ports[port]:
            self.serial_ports[port][baudrate] = await self.init_serial_port(port, baudrate)
        
        reader, writer = self.serial_ports[port][baudrate]
        
        self.logger.info(f"Executing command on port {port}, baud rate {baudrate}")
        
        # Use the baud rate from the client request
        baud_rate = command_info['baudrate']
        char_time = 11 / baud_rate  # 11 bits per character (1 start + 8 data + 1 stop + 1 parity)

        # Record start time
        start_time = asyncio.get_event_loop().time()

        # Send command
        self.logger.info(f"Sending {len(command_info['command'])} bytes to {port}: {' '.join(f'{b:02X}' for b in command_info['command'])}")
        self.logger.info(f"Expected response length: {command_info['response_length']} bytes")
        writer.write(command_info['command'])
        await writer.drain()

        # Read response
        try:
            response = bytearray()
            remaining_length = command_info['response_length']
            timeout = command_info['timeout']

            iteration = 0
            while remaining_length > 0 and timeout > 0:
                iteration += 1
                self.logger.info(f"Starting iteration {iteration} for command on port {port}")
                await asyncio.sleep(3.5 * char_time) # Wait for 3.5 character times between reads
                read_start = asyncio.get_event_loop().time()
                chunk = await asyncio.wait_for(reader.read(remaining_length), timeout=timeout)
                read_end = asyncio.get_event_loop().time()
                
                # Only take the remaining length bytes if the chunk is larger
                chunk = chunk[:remaining_length]
                response.extend(chunk)
                remaining_length -= len(chunk)
                timeout -= (read_end - read_start)

                if remaining_length > 0:
                    self.logger.info(f"Partial response received. Waiting for {remaining_length} more bytes.")
                    await asyncio.sleep(3.5 * char_time)  # Wait for 3.5 character times between reads
                
                self.logger.info(f"Received {len(response)} bytes from {port}: {' '.join(f'{b:02X}' for b in response)}")

            if remaining_length > 0:
                raise asyncio.TimeoutError("Timed out while waiting for complete response")

            self.logger.info(f"Received {len(response)} bytes from {port}: {' '.join(f'{b:02X}' for b in response)}")
            
            # Record end time and calculate total time
            end_time = asyncio.get_event_loop().time()
            total_time = end_time - start_time
            self.logger.info(f"Total time from sending to receiving full response: {total_time:.6f} seconds")
            
            # Check if the first two bytes of the response match the command
            if len(response) >= 2 and response[:2] == command_info['command'][:2]:
                # Send response back to client as a hexadecimal string
                hex_response = response.hex()
                client_response = f"{command_info['command_id']}:{hex_response}\n"
                command_info['writer'].write(client_response.encode())
                await command_info['writer'].drain()
                self.logger.info(f"Sent {len(response)} bytes to client {command_info['client_id']}")
            
        except asyncio.TimeoutError:
            # Record end time and calculate total time for timeout case
            end_time = asyncio.get_event_loop().time()
            total_time = end_time - start_time
            self.logger.warning(f"Timeout waiting for response on port {port}. Total time: {total_time:.6f} seconds")

        # After reading the expected response or timing out, clear any remaining data
        await self.clear_buffer(reader)

    async def clear_buffer(self, reader):
        # Wait a short time for any remaining data to arrive
        await asyncio.sleep(0.1)
        
        # Read and discard any remaining data
        try:
            while True:
                chunk = await asyncio.wait_for(reader.read(100), timeout=0.1)
                if not chunk:
                    break
                self.logger.warning(f"Cleared {len(chunk)} extra bytes from buffer")
        except asyncio.TimeoutError:
            pass  # No more data to read

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
        self.logger.info("Server shutdown initiated")
        asyncio.run(server.stop())
