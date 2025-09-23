import asyncio
from LuminaLogger import LuminaLogger
import time
from collections import defaultdict

class RetryManager:
    """
    Manages automatic retries for failed operations with exponential backoff.

    Attributes:
        max_retries (int): Maximum number of retry attempts
        backoff_factor (float): Multiplier for exponential backoff calculation
        retry_counts (dict): Tracks retry attempts per command
        logger (LuminaLogger): Logger instance for retry operations
    """

    def __init__(self, max_retries=3, backoff_factor=1.5):
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.retry_counts = {}
        self.logger = LuminaLogger("RetryManager")

    async def execute_with_retry(self, func, *args, **kwargs):
        """
        Execute a function with automatic retry on failure.

        Args:
            func (callable): Async function to execute
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            Any: Result of the successful function execution

        Raises:
            Exception: Last encountered exception after all retries fail
        """

        attempt = 0
        last_exception = None
        command_id = kwargs.get('command_id', 'unknown')
        
        while attempt < self.max_retries:
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                attempt += 1
                wait_time = self.backoff_factor ** attempt
                self.logger.warning(f"Attempt {attempt} failed for command {command_id}: {str(e)}")
                self.retry_counts[command_id] = attempt
                
                if attempt < self.max_retries:
                    self.logger.info(f"Retrying in {wait_time:.2f} seconds...")
                    await asyncio.sleep(wait_time)
                last_exception = e
        
        raise last_exception

class CommandPrioritizer:
    """
    Manages command queues with different priority levels.

    Attributes:
        high_priority_queue (PriorityQueue): Queue for high priority commands
        normal_priority_queue (Queue): Queue for normal priority commands
        low_priority_queue (Queue): Queue for low priority commands
        logger (LuminaLogger): Logger for queue operations
    """

    def __init__(self):
        self.high_priority_queue = asyncio.PriorityQueue()
        self.normal_priority_queue = asyncio.Queue()
        self.low_priority_queue = asyncio.Queue()
        self.logger = LuminaLogger("CommandPrioritizer")

    async def enqueue_command(self, priority, command):
        """
        Enqueue a command with specified priority.

        Args:
            priority (str): Priority level ("high", "normal", or "low")
            command (dict): Command data to enqueue
        """

        if priority == "high":
            await self.high_priority_queue.put((1, command))
        elif priority == "normal":
            await self.normal_priority_queue.put(command)
        else:
            await self.low_priority_queue.put(command)

    async def get_next_command(self):
        """
        Get the next command respecting priority order.

        Returns:
            dict: Next command to execute
        """

        if not self.high_priority_queue.empty():
            _, command = await self.high_priority_queue.get()
            return command
        if not self.normal_priority_queue.empty():
            return await self.normal_priority_queue.get()
        return await self.low_priority_queue.get()

class PerformanceMonitor:
    """
    Tracks and analyzes command execution performance metrics.

    Attributes:
        command_timings (dict): Execution times for each command
        port_statistics (dict): Performance statistics per port
        logger (LuminaLogger): Logger for performance monitoring
        start_time (float): Monitor start timestamp
    """

    def __init__(self):
        self.command_timings = {}
        self.port_statistics = {}
        self.logger = LuminaLogger("PerformanceMonitor")
        self.start_time = time.time()

    def record_command_execution(self, port, command_id, execution_time):
        """
        Record execution time for a command.

        Args:
            port (str): Port identifier
            command_id (str): Command identifier
            execution_time (float): Execution duration in seconds
        """

        if port not in self.port_statistics:
            self.port_statistics[port] = {
                'total_commands': 0,
                'total_time': 0,
                'min_time': float('inf'),
                'max_time': 0,
                'failures': 0
            }
        
        stats = self.port_statistics[port]
        stats['total_commands'] += 1
        stats['total_time'] += execution_time
        stats['min_time'] = min(stats['min_time'], execution_time)
        stats['max_time'] = max(stats['max_time'], execution_time)
        
        self.command_timings[command_id] = execution_time

    def get_port_performance_report(self):
        """
        Generate performance report for all ports.

        Returns:
            dict: Performance metrics per port
        """

        report = {}
        for port, stats in self.port_statistics.items():
            if stats['total_commands'] > 0:
                avg_time = stats['total_time'] / stats['total_commands']
                report[port] = {
                    'average_execution_time': avg_time,
                    'min_execution_time': stats['min_time'],
                    'max_execution_time': stats['max_time'],
                    'total_commands': stats['total_commands'],
                    'failure_rate': stats['failures'] / stats['total_commands']
                }
        return report

class CommandValidator:
    """
    Validates command structures against predefined patterns.

    Attributes:
        logger (LuminaLogger): Logger for validation operations
        valid_commands (set): Set of valid command names
        command_patterns (dict): Patterns for command validation
    """

    def __init__(self):
        self.logger = LuminaLogger("CommandValidator")
        self.valid_commands = set()
        self.command_patterns = {}
        
    def register_command_pattern(self, command_name, pattern):
        """
        Register a new command pattern.

        Args:
            command_name (str): Name of the command
            pattern (bytes): Expected byte pattern
        """

        self.command_patterns[command_name] = pattern
        
    def validate_command(self, command_data):
        """
        Validate a command against its registered pattern.

        Args:
            command_data (dict): Command data including name and bytes

        Returns:
            bool: True if valid, False otherwise

        Raises:
            ValueError: If command type unknown or pattern mismatch
        """

        command_type = command_data.get('name')
        if command_type not in self.command_patterns:
            raise ValueError(f"Unknown command type: {command_type}")
            
        pattern = self.command_patterns[command_type]
        command_bytes = command_data.get('command')
        
        if not self._validate_pattern(command_bytes, pattern):
            raise ValueError(f"Command does not match pattern for {command_type}")
            
        return True
        
    def _validate_pattern(self, command_bytes, pattern):
        if len(command_bytes) != len(pattern):
            return False
            
        for byte, pattern_byte in zip(command_bytes, pattern):
            if pattern_byte != 'x' and byte != pattern_byte:
                return False
                
        return True

class ConnectionPool:
    """
    Manages a pool of reusable connections.

    Attributes:
        max_connections (int): Maximum number of concurrent connections
        active_connections (dict): Currently active connections
        connection_queue (Queue): Queue for connection requests
        logger (LuminaLogger): Logger for connection operations
    """

    def __init__(self, max_connections=10):
        self.max_connections = max_connections
        self.active_connections = {}
        self.connection_queue = asyncio.Queue()
        self.logger = LuminaLogger("ConnectionPool")
        
    async def get_connection(self, port, baudrate):
        """
        Get or create a connection for specified port and baudrate.

        Args:
            port (str): Port identifier
            baudrate (int): Communication baudrate

        Returns:
            Connection: Active connection object
        """

        connection_key = f"{port}:{baudrate}"
        
        if connection_key in self.active_connections:
            return self.active_connections[connection_key]
            
        if len(self.active_connections) >= self.max_connections:
            await self._cleanup_inactive_connections()
            
        connection = await self._create_new_connection(port, baudrate)
        self.active_connections[connection_key] = connection
        return connection
        
    async def _cleanup_inactive_connections(self):
        for key, conn in list(self.active_connections.items()):
            if not conn.is_active():
                await conn.close()
                del self.active_connections[key]
                
    async def _create_new_connection(self, port, baudrate):
        # Connection creation logic here
        pass

class DeviceStateManager:
    """
    Manages and tracks device state changes.

    Attributes:
        device_states (dict): Current state of each device
        state_history (defaultdict): Historical state changes
        state_locks (dict): Locks for thread-safe state updates
        logger (LuminaLogger): Logger for state changes
    """

    def __init__(self):
        self.device_states = {}
        self.state_history = defaultdict(list)
        self.state_locks = {}
        self.logger = LuminaLogger("DeviceStateManager")
        
    async def update_state(self, device_id, new_state):
        """
        Update device state with change tracking.

        Args:
            device_id (str): Device identifier
            new_state (dict): New device state
        """

        if device_id not in self.state_locks:
            self.state_locks[device_id] = asyncio.Lock()
            
        async with self.state_locks[device_id]:
            old_state = self.device_states.get(device_id, {})
            self.device_states[device_id] = new_state
            self.state_history[device_id].append({
                'timestamp': time.time(),
                'old_state': old_state,
                'new_state': new_state
            })
            
            if len(self.state_history[device_id]) > 100:
                self.state_history[device_id].pop(0)
                
            await self._notify_state_change(device_id, old_state, new_state)
            
    async def _notify_state_change(self, device_id, old_state, new_state):
        # Notification logic for state changes
        changes = self._compute_state_changes(old_state, new_state)
        if changes:
            self.logger.info(f"Device {device_id} state changes: {changes}")
            
    def _compute_state_changes(self, old_state, new_state):
        changes = {}
        for key in set(old_state.keys()) | set(new_state.keys()):
            if old_state.get(key) != new_state.get(key):
                changes[key] = {
                    'old': old_state.get(key),
                    'new': new_state.get(key)
                }
        return changes

class QueueAnalytics:
    """
    Analyzes queue performance and metrics.

    Attributes:
        queue_metrics (defaultdict): Metrics for each queue
        logger (LuminaLogger): Logger for analytics
    """

    def __init__(self):
        self.queue_metrics = defaultdict(lambda: {
            'enqueued': 0,
            'dequeued': 0,
            'processing_times': [],
            'wait_times': [],
            'errors': 0
        })
        self.logger = LuminaLogger("QueueAnalytics")
        
    def record_enqueue(self, queue_name, command_id):
        """
        Record command enqueue event.

        Args:
            queue_name (str): Queue identifier
            command_id (str): Command identifier
        """

        self.queue_metrics[queue_name]['enqueued'] += 1
        self.queue_metrics[queue_name]['current_queue_size'] = (
            self.queue_metrics[queue_name]['enqueued'] - 
            self.queue_metrics[queue_name]['dequeued']
        )
        
    def record_dequeue(self, queue_name, command_id, wait_time):
        self.queue_metrics[queue_name]['dequeued'] += 1
        self.queue_metrics[queue_name]['wait_times'].append(wait_time)
        self.queue_metrics[queue_name]['current_queue_size'] = (
            self.queue_metrics[queue_name]['enqueued'] - 
            self.queue_metrics[queue_name]['dequeued']
        )
        
    def record_processing_time(self, queue_name, processing_time):
        self.queue_metrics[queue_name]['processing_times'].append(processing_time)
        
    def get_queue_statistics(self, queue_name):
        """
        Get statistics for specified queue.

        Args:
            queue_name (str): Queue identifier

        Returns:
            dict: Queue performance metrics
        """

        metrics = self.queue_metrics[queue_name]
        wait_times = metrics['wait_times']
        processing_times = metrics['processing_times']
        
        return {
            'total_commands': metrics['enqueued'],
            'current_queue_size': metrics['current_queue_size'],
            'average_wait_time': sum(wait_times) / len(wait_times) if wait_times else 0,
            'average_processing_time': sum(processing_times) / len(processing_times) if processing_times else 0,
            'max_wait_time': max(wait_times) if wait_times else 0,
            'max_processing_time': max(processing_times) if processing_times else 0
        }

class ResponseParser:
    """
    Parses device responses according to registered patterns.

    Attributes:
        response_patterns (dict): Patterns for response parsing
        logger (LuminaLogger): Logger for parsing operations
    """

    def __init__(self):
        self.response_patterns = {}
        self.logger = LuminaLogger("ResponseParser")
        
    def register_response_pattern(self, command_type, pattern):
        self.response_patterns[command_type] = pattern
        
    def parse_response(self, command_type, response_bytes):
        """
        Parse response bytes according to command pattern.

        Args:
            command_type (str): Type of command
            response_bytes (bytes): Raw response data

        Returns:
            dict: Parsed response data

        Raises:
            ValueError: If no pattern registered or parsing fails
        """

        if command_type not in self.response_patterns:
            raise ValueError(f"No response pattern registered for {command_type}")
            
        pattern = self.response_patterns[command_type]
        parsed_data = {}
        
        try:
            index = 0
            for field_name, field_type, field_length in pattern:
                field_bytes = response_bytes[index:index + field_length]
                parsed_data[field_name] = self._parse_field(field_bytes, field_type)
                index += field_length
                
        except Exception as e:
            self.logger.error(f"Error parsing response for {command_type}: {str(e)}")
            raise
            
        return parsed_data
        
    def _parse_field(self, field_bytes, field_type):
        if field_type == 'int':
            return int.from_bytes(field_bytes, byteorder='big')
        elif field_type == 'ascii':
            return field_bytes.decode('ascii')
        elif field_type == 'hex':
            return field_bytes.hex()
        elif field_type == 'bool':
            return bool(field_bytes[0])
        else:
            raise ValueError(f"Unknown field type: {field_type}")

class DeviceConfigManager:
    """
    Manages device configurations and validation.

    Attributes:
        device_configs (dict): Device configurations
        config_validators (dict): Validation schemas
        logger (LuminaLogger): Logger for configuration operations
    """

    def __init__(self):
        self.device_configs = {}
        self.config_validators = {}
        self.logger = LuminaLogger("DeviceConfigManager")
        
    def register_device_config(self, device_type, config_schema):
        self.config_validators[device_type] = config_schema
        
    async def set_device_config(self, device_id, device_type, config):
        """
        Set and validate device configuration.

        Args:
            device_id (str): Device identifier
            device_type (str): Type of device
            config (dict): Configuration data

        Raises:
            ValueError: If configuration invalid or schema missing
        """

        if device_type not in self.config_validators:
            raise ValueError(f"No configuration schema for device type: {device_type}")
            
        validator = self.config_validators[device_type]
        if not self._validate_config(config, validator):
            raise ValueError("Invalid configuration")
            
        self.device_configs[device_id] = {
            'type': device_type,
            'config': config,
            'last_updated': time.time()
        }
        
        await self._apply_config(device_id, config)
        
    def _validate_config(self, config, validator):
        try:
            for key, value_type in validator.items():
                if key not in config:
                    return False
                if not isinstance(config[key], value_type):
                    return False
            return True
        except Exception as e:
            self.logger.error(f"Configuration validation error: {str(e)}")
            return False
            
    async def _apply_config(self, device_id, config):
        # Logic to apply configuration to device
        self.logger.info(f"Applying configuration to device {device_id}")
        # Implementation details here
