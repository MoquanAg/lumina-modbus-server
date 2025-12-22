"""
Basic tests for LuminaLogger module.
"""

import os
import tempfile
import shutil
import pytest
from LuminaLogger import LuminaLogger


class TestLuminaLogger:
    """Test cases for LuminaLogger class."""

    def setup_method(self):
        """Set up test fixtures before each test method."""
        # Create a temporary directory for test logs
        self.test_log_dir = tempfile.mkdtemp()
        self.logger = LuminaLogger("TestLogger", log_dir=self.test_log_dir)

    def teardown_method(self):
        """Clean up after each test method."""
        # Remove temporary directory and all its contents
        if os.path.exists(self.test_log_dir):
            shutil.rmtree(self.test_log_dir)

    def test_logger_initialization(self):
        """Test that logger initializes correctly."""
        assert self.logger.name == "TestLogger"
        assert self.logger.log_dir == self.test_log_dir
        assert self.logger.logger is not None
        assert self.logger.max_file_size == 5 * 1024 * 1024  # 5 MB
        assert self.logger.max_total_size == 20 * 1024 * 1024  # 20 MB

    def test_log_dir_creation(self):
        """Test that log directory is created on initialization."""
        assert os.path.exists(self.test_log_dir)

    def test_debug_logging(self):
        """Test debug level logging."""
        test_message = "This is a debug message"
        self.logger.debug(test_message)
        # Logger should not raise exception
        assert True

    def test_info_logging(self):
        """Test info level logging."""
        test_message = "This is an info message"
        self.logger.info(test_message)
        # Logger should not raise exception
        assert True

    def test_warning_logging(self):
        """Test warning level logging."""
        test_message = "This is a warning message"
        self.logger.warning(test_message)
        # Logger should not raise exception
        assert True

    def test_error_logging(self):
        """Test error level logging."""
        test_message = "This is an error message"
        self.logger.error(test_message)
        # Logger should not raise exception
        assert True

    def test_critical_logging(self):
        """Test critical level logging."""
        test_message = "This is a critical message"
        self.logger.critical(test_message)
        # Logger should not raise exception
        assert True

    def test_get_total_log_size(self):
        """Test calculation of total log size."""
        initial_size = self.logger.get_total_log_size()
        assert isinstance(initial_size, int)
        assert initial_size >= 0

    def test_log_file_creation(self):
        """Test that log files are created when logging."""
        self.logger.info("Test message")
        # Check that log files exist in the directory
        log_files = [f for f in os.listdir(self.test_log_dir) if f.endswith('.log')]
        assert len(log_files) > 0

