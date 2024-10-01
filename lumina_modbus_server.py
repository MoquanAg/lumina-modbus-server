import asyncio
import logging
from asyncio import Queue
import serial_asyncio

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
				logging.info(f"Serial connection established on port {port} with baud rate {baud_rate}")
			except Exception as e:
				logging.error(f"Failed to create serial connection on port {port}: {str(e)}")
				raise

	async def process_commands(self, port):
		reader, writer = self.serial_connections[port]
		while True:
			try:
				command = await self.command_queues[port].get()
				writer.write(command)
				await writer.drain()
				response = await reader.read(100)  # Adjust buffer size as needed
				await self.response_queues[port].put(response)
			except Exception as e:
				logging.error(f"Error processing command on port {port}: {str(e)}")

	async def send_command(self, port, command):
		if port not in self.serial_connections:
			raise ValueError(f"Port {port} is not initialized")
		
		await self.command_queues[port].put(command)
		response = await self.response_queues[port].get()
		return response

	async def run(self):
		self.running = True
		logging.info("LuminaModbusServer is running and waiting for client connections...")
		while self.running:
			await asyncio.sleep(1)

# Run the server when the script is executed directly
if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	server = LuminaModbusServer()
	asyncio.run(server.run())
