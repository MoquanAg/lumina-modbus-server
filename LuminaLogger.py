import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime

class LuminaLogger:
    def __init__(self, name, log_dir='logs'):
        self.name = name
        self.log_dir = log_dir
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        self.current_log_file = None
        self.setup_logger()

    def setup_logger(self):
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
        # Remove old file handler if exists
        if self.current_log_file:
            self.logger.removeHandler(self.current_log_file)

        # Create new log file name
        current_date = datetime.now().strftime('%Y-%m-%d')
        log_file_name = f"{current_date}.log"
        log_file_path = os.path.join(self.log_dir, log_file_name)

        # Create rotating file handler
        file_handler = RotatingFileHandler(
            log_file_path,
            maxBytes=1024 * 1024,  # 1 MB
            backupCount=0  # No backups, new file will be created
        )
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)

        # Add file handler to logger
        self.logger.addHandler(file_handler)
        self.current_log_file = file_handler

    def check_and_rotate_log(self):
        current_date = datetime.now().strftime('%Y-%m-%d')
        if self.current_log_file.baseFilename != os.path.join(self.log_dir, f"{current_date}.log"):
            self.create_new_log_file()

    def debug(self, message):
        self.check_and_rotate_log()
        self.logger.debug(message)

    def info(self, message):
        self.check_and_rotate_log()
        self.logger.info(message)

    def warning(self, message):
        self.check_and_rotate_log()
        self.logger.warning(message)

    def error(self, message):
        self.check_and_rotate_log()
        self.logger.error(message)

    def critical(self, message):
        self.check_and_rotate_log()
        self.logger.critical(message)

