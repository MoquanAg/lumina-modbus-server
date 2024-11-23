"""
LuminaLogger: A robust logging utility for managing rotational logs with size limits.
Provides daily log rotation and size-based splitting with consistent formatting.

Features:
- Automatic log rotation based on date and size
- Console and file logging
- Configurable log directory and file size limits
- Standard logging levels (DEBUG, INFO, WARNING, ERROR, CRITICAL)
"""

import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime

class LuminaLogger:
    """
    A robust logging utility for managing rotational logs with size limits.
    
    Attributes:
        name (str): Logger identifier name
        log_dir (str): Directory path for log files
        logger (logging.Logger): Python logger instance
        current_log_file (RotatingFileHandler): Current file handler
        max_file_size (int): Maximum size of each log file in bytes
    """

    def __init__(self, name, log_dir='logs'):
        """
        Initialize the logger with name and directory.

        Args:
            name (str): Name identifier for the logger
            log_dir (str): Directory path for storing log files, defaults to 'logs'
        """
        self.name = name
        self.log_dir = log_dir
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        self.current_log_file = None
        self.max_file_size = 5 * 1024 * 1024  # 5 MB
        self.setup_logger()

    def setup_logger(self):
        """
        Configure the logger with console and file handlers.
        Creates log directory and initializes handlers with formatters.
        """
        # Create logs directory if it doesn't exist
        os.makedirs(self.log_dir, exist_ok=True)

        # Create a new log file
        self.create_new_log_file()

        # Create console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(console_formatter)

        # Add handlers to logger
        self.logger.addHandler(console_handler)

    def create_new_log_file(self):
        """
        Create a new log file and set up its handler.
        Removes old file handler if exists and creates a new one with rotation settings.
        """
        # Remove old file handler if exists
        if self.current_log_file:
            self.logger.removeHandler(self.current_log_file)

        # Create new log file name
        current_date = datetime.now().strftime('%Y-%m-%d')
        log_file_path = self.get_available_log_file_path(current_date)

        # Create rotating file handler
        file_handler = RotatingFileHandler(
            log_file_path,
            maxBytes=self.max_file_size,
            backupCount=0  # No backups, new file will be created with suffix
        )
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)

        # Add file handler to logger
        self.logger.addHandler(file_handler)
        self.current_log_file = file_handler

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
        """
        current_date = datetime.now().strftime('%Y-%m-%d')
        current_log_path = self.current_log_file.baseFilename
        
        if os.path.getsize(current_log_path) >= self.max_file_size or \
           os.path.basename(current_log_path) != f"{current_date}.log":
            self.create_new_log_file()

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
