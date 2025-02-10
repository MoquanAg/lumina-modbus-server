"""
LuminaLogger: A robust logging utility for managing rotational logs with size limits.
Provides daily log rotation and size-based splitting with consistent formatting.

Features:
- Automatic log rotation based on date and size
- Console and file logging
- Configurable log directory and file size limits
- Standard logging levels (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- Total log size limit with automatic cleanup of oldest files
"""

import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
import glob

class LuminaLogger:
    """
    A robust logging utility for managing rotational logs with size limits.
    
    Attributes:
        name (str): Logger identifier name
        log_dir (str): Directory path for log files
        logger (logging.Logger): Python logger instance
        current_log_file (RotatingFileHandler): Current file handler
        max_file_size (int): Maximum size of each log file in bytes
        max_total_size (int): Maximum total size of all log files in bytes
    """

    def __init__(self, name, log_dir='logs'):
        """
        Initialize the logger with name and directory.

        Args:
            name (str): Name identifier for the logger
            log_dir (str): Directory path for storing log files, defaults to 'logs'
        """
        self.name = name
        # Use current directory as base
        self.log_dir = os.path.join(os.path.dirname(__file__), log_dir)
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        self.current_log_file = None
        self.max_file_size = 5 * 1024 * 1024  # 5 MB
        self.max_total_size = 20 * 1024 * 1024  # 20 MB
        self.setup_logger()

    def get_total_log_size(self):
        """
        Calculate the total size of all log files in the log directory.

        Returns:
            int: Total size in bytes
        """
        total_size = 0
        if os.path.exists(self.log_dir):
            for log_file in glob.glob(os.path.join(self.log_dir, "*.log")):
                total_size += os.path.getsize(log_file)
        return total_size

    def cleanup_old_logs(self):
        """
        Remove oldest log files when total size exceeds max_total_size.
        Files are removed in order of oldest to newest until total size is under limit.
        """
        if not os.path.exists(self.log_dir):
            return

        # Get list of all log files with their creation times
        log_files = []
        for log_file in glob.glob(os.path.join(self.log_dir, "*.log")):
            # Skip the current log file
            if self.current_log_file and log_file == self.current_log_file.baseFilename:
                continue
            creation_time = os.path.getctime(log_file)
            size = os.path.getsize(log_file)
            log_files.append((creation_time, log_file, size))

        # Sort by creation time (oldest first)
        log_files.sort()

        # Calculate current total size
        total_size = self.get_total_log_size()

        # Remove oldest files until we're under the limit
        for _, file_path, size in log_files:
            if total_size <= self.max_total_size:
                break
            try:
                os.remove(file_path)
                total_size -= size
                self.logger.info(f"Removed old log file: {os.path.basename(file_path)}")
            except OSError as e:
                self.logger.error(f"Error removing old log file {file_path}: {str(e)}")

    def setup_logger(self):
        """
        Configure the logger with console and file handlers.
        Creates log directory and initializes handlers with formatters.
        """
        # Create logs directory if it doesn't exist
        os.makedirs(self.log_dir, exist_ok=True)

        # Clear any existing handlers
        self.logger.handlers = []

        # Set root logger to DEBUG to ensure all logs are shown
        logging.getLogger().setLevel(logging.DEBUG)

        # Create a new log file
        self.create_new_log_file()

        # Create console handler with DEBUG level (show all logs)
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)  # Set to DEBUG to show all logs
        console_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(console_formatter)

        # Add handlers to logger
        self.logger.addHandler(console_handler)
        
        # Ensure propagation is enabled for complete logging
        self.logger.propagate = True

        # Initial cleanup of old logs
        self.cleanup_old_logs()

    def create_new_log_file(self):
        """
        Create a new log file and set up its handler.
        Removes old file handler if exists and creates a new one with rotation settings.
        """
        # Remove old file handler if exists and close it properly
        if self.current_log_file:
            self.logger.removeHandler(self.current_log_file)
            self.current_log_file.close()  # Close the file handler explicitly

        # Create new log file name
        current_date = datetime.now().strftime('%Y-%m-%d')
        log_file_path = self.get_available_log_file_path(current_date)

        # Create rotating file handler with DEBUG level
        file_handler = logging.FileHandler(log_file_path)
        file_handler.setLevel(logging.DEBUG)  # Ensure DEBUG level for file handler
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)

        # Add file handler to logger
        self.logger.addHandler(file_handler)
        self.current_log_file = file_handler

        # Check and cleanup old logs if needed
        self.cleanup_old_logs()

    def get_available_log_file_path(self, current_date):
        """
        Generate an available file path for new log file.

        Args:
            current_date (str): Date string in YYYY-MM-DD format

        Returns:
            str: Available file path for the new log file
        """
        base_name = f"{current_date}.log"
        log_file_path = os.path.join(self.log_dir, base_name)
        suffix = 1

        while os.path.exists(log_file_path) and os.path.getsize(log_file_path) >= self.max_file_size:
            log_file_path = os.path.join(self.log_dir, f"{current_date}_{suffix}.log")
            suffix += 1

        return log_file_path

    def check_and_rotate_log(self):
        """
        Check if log rotation is needed and perform rotation if necessary.
        Rotation occurs when current file size exceeds limit or date changes.
        Also checks total log size and triggers cleanup if needed.
        """
        if not self.current_log_file:
            self.create_new_log_file()
            return

        current_date = datetime.now().strftime('%Y-%m-%d')
        current_log_path = self.current_log_file.baseFilename
        
        # Check if rotation is needed
        if os.path.getsize(current_log_path) >= self.max_file_size or \
           os.path.basename(current_log_path) != f"{current_date}.log":
            self.create_new_log_file()

        # Check total size and cleanup if needed
        if self.get_total_log_size() > self.max_total_size:
            self.cleanup_old_logs()

    def debug(self, message):
        """
        Log a debug level message.

        Args:
            message (str): Debug message to log
        """
        self.check_and_rotate_log()
        self.logger.debug(message)

    def info(self, message):
        """
        Log an info level message.

        Args:
            message (str): Info message to log
        """
        self.check_and_rotate_log()
        self.logger.info(message)

    def warning(self, message):
        """
        Log a warning level message.

        Args:
            message (str): Warning message to log
        """
        self.check_and_rotate_log()
        self.logger.warning(message)

    def error(self, message):
        """
        Log an error level message.

        Args:
            message (str): Error message to log
        """
        self.check_and_rotate_log()
        self.logger.error(message)

    def critical(self, message):
        """
        Log a critical level message.

        Args:
            message (str): Critical message to log
        """
        self.check_and_rotate_log()
        self.logger.critical(message)
