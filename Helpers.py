"""
Simplified Helpers: Essential utilities for Modbus communication.
Removed over-engineering, kept only what's actually needed.
"""

import time
from LuminaLogger import LuminaLogger

class SimpleRetryManager:
    """
    Simple retry logic for failed operations.
    """
    
    def __init__(self, max_retries=3, delay=1.0):
        self.max_retries = max_retries
        self.delay = delay
        self.logger = LuminaLogger("SimpleRetryManager")
    
    def execute_with_retry(self, func, *args, **kwargs):
        """
        Execute a function with simple retry logic.
        
        Args:
            func: Function to execute
            *args: Function arguments
            **kwargs: Function keyword arguments
            
        Returns:
            Function result
            
        Raises:
            Exception: Last exception after all retries fail
        """
        last_exception = None
        
        for attempt in range(self.max_retries + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                if attempt < self.max_retries:
                    self.logger.warning(f"Attempt {attempt + 1} failed: {str(e)}")
                    time.sleep(self.delay)
                else:
                    self.logger.error(f"All {self.max_retries + 1} attempts failed")
        
        raise last_exception

class CommandValidator:
    """
    Basic command validation.
    """
    
    @staticmethod
    def validate_modbus_command(command: bytes, min_length=4, max_length=256):
        """
        Validate basic Modbus command structure.
        
        Args:
            command: Command bytes to validate
            min_length: Minimum command length
            max_length: Maximum command length
            
        Returns:
            bool: True if valid, False otherwise
        """
        if not isinstance(command, bytes):
            return False
        
        if len(command) < min_length or len(command) > max_length:
            return False
        
        # Basic Modbus validation: first byte should be device address (1-247)
        if command[0] < 1 or command[0] > 247:
            return False
        
        return True

class ResponseParser:
    """
    Basic response parsing utilities.
    """
    
    @staticmethod
    def parse_modbus_response(response: bytes, expected_length=None):
        """
        Parse Modbus response and extract data.
        
        Args:
            response: Response bytes
            expected_length: Expected response length
            
        Returns:
            dict: Parsed response data
        """
        if not response:
            return {"error": "Empty response"}
        
        if expected_length and len(response) != expected_length:
            return {"error": f"Length mismatch: expected {expected_length}, got {len(response)}"}
        
        try:
            # Basic Modbus response parsing
            device_address = response[0]
            function_code = response[1]
            
            # Extract data (skip address, function code, and CRC)
            data = response[2:-2] if len(response) > 4 else response[2:]
            
            return {
                "device_address": device_address,
                "function_code": function_code,
                "data": data,
                "length": len(response)
            }
        except Exception as e:
            return {"error": f"Parse error: {str(e)}"}

# Keep only essential utilities - remove all the over-engineered classes
