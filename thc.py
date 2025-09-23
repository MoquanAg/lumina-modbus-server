if __name__ == '__main__':
    from modbus_helpers import *
else:
    from hardware.modbus_helpers import *

import serial
import time
import os, sys
current_dir = os.path.dirname(__file__)
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)
import GLOBALS
logger = GLOBALS.logger

import math
import helpers
from lumina_modbus_event_emitter import ModbusResponse

class THC:

    _instances = {}  # Dictionary to hold multiple instances
    modbus_client = None  # Class variable to hold the LuminaModbusClient instance

    @classmethod
    def load_all_sensors(cls, port='/dev/ttyAMA2'):
        """
        Load and initialize all THC sensors defined in the configuration file.
        """
        config = GLOBALS.DEVICE_CONFIG_FILE
        
        try:
            if 'DEVICE' in config:
                for key, value in config['DEVICE'].items():
                    if key.upper().startswith("THC_"):
                        sensor_id = "_".join(key.split("_")[1:])
                        instance = cls(sensor_id, port)
                        logger.info(f"Loaded sensor with ID: {sensor_id}")
            else:
                logger.info("No 'DEVICE' section found in the configuration file.")
        except Exception as e:
            logger.info(f"Failed to load sensors: {e}")

    @classmethod
    def get_statuses(cls):
        for _, sensor_instance in THC._instances.items():
            # print(sensor_instance.sensor_id )
            sensor_instance.get_status()
            time.sleep(0.01)
            
    @classmethod
    def get_statuses_async(cls):
        """
        Asynchronously get status from all THC sensors.
        Each sensor's command is queued with the modbus client and responses
        are handled via the event emitter callback.
        """
        for _, sensor_instance in THC._instances.items():
            sensor_instance.get_status_async()
            time.sleep(0.01)  # Small delay between sensors

    @classmethod
    def get_temperatures(cls):
        temps = []
        for _, sensor_instance in THC._instances.items():
            # print(sensor_instance.sensor_id )
            sensor_instance.get_status()
            if sensor_instance.position == "main_upper":
                temps.append(sensor_instance.temperature)
            time.sleep(0.01)
        return temps
    
    @classmethod
    def get_humidities(cls):
        humidities = []
        for _, sensor_instance in THC._instances.items():
            # print(sensor_instance.sensor_id )
            sensor_instance.get_status()
            if sensor_instance.position == "main_upper":
                humidities.append(sensor_instance.humidity)
            time.sleep(0.01)
        return humidities
    
    @classmethod
    def get_co2_levels(cls):
        co2_levels = []
        for _, sensor_instance in THC._instances.items():
            # print(sensor_instance.sensor_id )
            sensor_instance.get_status()
            co2_levels.append(sensor_instance.co2)
            time.sleep(0.01)
        return co2_levels


    def __new__(cls, sensor_id, *args, **kwargs):
        if sensor_id not in cls._instances:
            logger.debug(f"Creating the THC instance for {sensor_id}.")
            instance = super(THC, cls).__new__(cls)
            instance.init(sensor_id, *args, **kwargs)  # Initialize the instance
            cls._instances[sensor_id] = instance
        return cls._instances[sensor_id]

    def init(self, sensor_id, port='/dev/ttyAMA2'):
        logger.info(f"Initializing the THC instance for {sensor_id} in {port}.")
        self.sensor_id = sensor_id
        self.port = port
        self.data_path = GLOBALS.SAVED_SENSOR_DATA_PATH
        self.address = 0x02  # Default address
        self.ser = serial.Serial()
        self.baud_rate = 9600 
        self.position = "main"
        self.temperature = None
        self.humidity = None
        self.co2 = None
        self.vpd = None
        self.last_updated = None
        self.load_address()
        self.modbus_client = GLOBALS.modbus_client
        self.modbus_client.event_emitter.subscribe('THC', self._handle_response)
        self.pending_commands = {}

    def _handle_response(self, response: ModbusResponse) -> None:
        """
        Handle responses from the modbus client event emitter.
        Called automatically when a response is received for this sensor.
        """
        # Only process responses for commands that this instance sent
        if response.command_id not in self.pending_commands:
            return
            
        logger.info(f"Received response for {self.sensor_id} with UUID: {response.command_id}")
        
        # Get the original command timestamp
        command_timestamp = self.pending_commands[response.command_id].get('timestamp')
        if command_timestamp:
            elapsed_time = response.timestamp - command_timestamp
            logger.debug(f"Response time for {self.sensor_id}: {elapsed_time:.3f}s")
        
        if response.status == 'success':
            self._process_status_response(response.data)
        elif response.status in ['timeout', 'error', 'connection_lost']:
            logger.warning(f"Command failed with status {response.status} for UUID: {response.command_id}")
            self.save_null_data()
        
        # Clean up the pending command after processing
        del self.pending_commands[response.command_id]

    def _process_status_response(self, response):
        """Process the raw response data from the sensor."""
        if response and len(response) == 11:  # Expected response length
            rh = int.from_bytes(response[3:5], byteorder='big') / 1000
            temp = int.from_bytes(response[5:7], byteorder='big') / 10
            co2 = int.from_bytes(response[7:9], byteorder='big')

            if rh > 1 or rh <= 0 or temp >= 100 or temp <= -50 or co2 > 5000 or co2 <= 100:
                logger.info(f"Skipping {self.sensor_id}")
                logger.info(f"Invalid values: Temp={temp}°C, Humidity={rh*100:.2f}%, CO2={co2}ppm")
                self.save_null_data()
                return

            self.temperature = temp
            self.humidity = rh
            self.co2 = co2
            
            self.update_vpd()
            logger.info(f"{self.sensor_id} Temperature: {temp}°C, Humidity: {rh*100:.2f}%, CO2: {co2}ppm, VPD: {self.vpd:.2f}kPa")

            self.last_updated = helpers.datetime_to_iso8601()
            self.save_data()
        else:
            logger.info(f"Invalid response from {self.sensor_id} length: {len(response) if response else 0} while should be 11")
            self.save_null_data()

    def open_connection(self):
        self.ser = serial.Serial(self.port, self.baud_rate, serial.EIGHTBITS, serial.PARITY_NONE, serial.STOPBITS_ONE)
        
    def close_connection(self):
        self.ser.close()

    def load_address(self):
        config = GLOBALS.DEVICE_CONFIG_FILE
        try:
            if 'DEVICE' in config:
                for key, value in config['DEVICE'].items():
                    # logger.info(f"Key: {key}, Value: {value}, self.sensor_id: {self.sensor_id}")  # Debugging print statement
                    if key.upper().startswith(f"THC_{self.sensor_id.upper()}"):
                        self.address = int(value, 16)
                        self.position = self.sensor_id
                        logger.info(f"{key} address loaded: {hex(self.address)}")
                        break
                    # else:
                    #     logger.info(f"{self.sensor_id} address not found. Using default.")
        except FileNotFoundError:
            logger.info(f"Device config not found. Using default address.")
        except ValueError:
            logger.info(f"Invalid address format. Using default: {hex(self.address)}")

    def get_status(self):

        self.open_connection()
        
        # Commands:
        # Read THC: 0x01, 0x03, 0x00, 0x00, 0x00, 0x03
        # Set address to 0x02: 0x01, 0x06, 0x07, 0xD0, 0x00, 0x02

        # Read THC
        command = bytearray([self.address, 0x03, 0x00, 0x00, 0x00, 0x03])  # Specific command for your THC sensor
        # logger.info(f"Loading from {self.address} for {self.sensor_id}")
        response_length = 11 # Expected length of the response
        response = send_command(self, command, timeout=1.0)  # Added timeout parameter

        self.close_connection()

        if response is None:
            print("No response or timeout occurred")
            self.save_null_data()
            return False
        
        # if self.sensor_id == "main_upper":
        # logger.info(f"{self.sensor_id} response: {response.hex()}")

        if len(response) == response_length:
            # Interpret the response based on your sensor's protocol
            rh = int.from_bytes(response[3:5], byteorder='big') / 1000
            temp = int.from_bytes(response[5:7], byteorder='big') / 10
            co2 = int.from_bytes(response[7:9], byteorder='big')

            if rh > 1 or rh <= 0 or temp >= 100 or temp <= -50 or co2 > 5000 or co2 <= 100:
                logger.info(f"Skipping {self.sensor_id}")
                logger.info(f"Invalid values: Temp={temp}°C, Humidity={rh*100:.2f}%, CO2={co2}ppm")
                return False

            self.temperature = temp
            self.humidity = rh
            self.co2 = co2
            
            self.update_vpd()

            # if self.sensor_id == "main_upper":
            logger.info(f"{self.sensor_id} Temperature: {temp}°C, Humidity: {rh*100:.2f}%, CO2: {co2}ppm, VPD: {self.vpd:.2f}kPa")

            self.last_updated = helpers.datetime_to_iso8601()
            self.save_data()
            
            return True
        else:
            logger.info(f"Invalid response from {self.sensor_id} length: {len(response)} while should be {response_length}")
            self.save_null_data()
            return False
        
    def get_status_async(self):
        """
        Queue a status request command with the modbus client.
        The response will be handled by _handle_response via the event emitter.
        """
        if not self.modbus_client.is_connected:
            logger.warning(f"ModbusClient not connected for {self.sensor_id}, saving null data")
            self.save_null_data()
            return

        command = bytearray([self.address, 0x03, 0x00, 0x00, 0x00, 0x03])
        try:
            # logger.info(f"Sending command for {self.sensor_id}: {command.hex()}")
            command_id = self.modbus_client.send_command(
                device_type='THC',
                port=self.port,
                command=command,
                baudrate=self.baud_rate,
                response_length=11,
                timeout=2.0
            )
            # Track the pending command with timestamp
            self.pending_commands[command_id] = {
                'timestamp': time.time(),
                'timeout': 2.0
            }
            logger.info(f"Command queued for {self.sensor_id} with ID {command_id} ")
        except Exception as e:
            logger.error(f"Failed to send command for {self.sensor_id}: {e}")
            self.save_null_data()

    def save_null_data(self):
        self.temperature = None
        self.humidity = None
        self.co2 = None
        self.vpd = None
        self.last_updated = helpers.datetime_to_iso8601()
        self.save_data()

    def update_vpd(self):
        """
        Calculate the Vapour Pressure Deficit (VPD).
        
        Parameters:
        - temp_celsius: Temperature in degrees Celsius.
        - relative_humidity: Relative Humidity in percentage.
        
        Returns:
        - VPD in kilopascals (kPa).
        """
        # Calculate saturation vapor pressure (es) using the Tetens formula
        es = 0.6108 * math.exp((17.27 * self.temperature) / (self.temperature + 237.3))
        
        # Calculate actual vapor pressure (ea) using relative humidity
        ea = (self.humidity) * es
        
        # Calculate VPD
        self.vpd = es - ea

        self.vpd = round(self.vpd, 3)
        
        return

    def save_data(self):
        # Update the sensor-specific configuration in the file
        data = {
            'position': self.position,
            'temperature': format(self.temperature, '.1f') if self.temperature is not None else None,
            'humidity': format(self.humidity, '.3f') if self.humidity is not None else None,  # Format with 3 decimal places
            'co2': self.co2,
            'vpd': format(self.vpd, '.3f') if self.vpd is not None else None,
            'last_updated': self.last_updated
        }

        helpers.save_sensor_data(['data', 'thc', self.sensor_id], data)
        
        logger.log_sensor_data(['data', 'thc', self.sensor_id], data)


def main():
    THC.load_all_sensors(port='/dev/ttyAMA2')
    iterations = 0
    max_iterations = 10  # Set the number of iterations you want
    
    while iterations < max_iterations:
        THC.get_statuses_async()
        time.sleep(0.1)  # Increased from 2 to 5 seconds to give more time between readings
        logger.info("\n\n")
        iterations += 1
        
    time.sleep(1)
    logger.info(f"Completed {max_iterations} iterations, stopping.")

if __name__ == "__main__":
    main()