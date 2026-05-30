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

    def test_each_logger_writes_to_its_own_named_file(self):
        """Two loggers in one dir must use separate, name-scoped files.

        Regression for the fleet disk-fill bug: instances previously shared
        date-named files, so they rotated/deleted each other's open logs.
        """
        d = tempfile.mkdtemp()
        try:
            alpha = LuminaLogger("alpha", log_dir=d)
            beta = LuminaLogger("beta", log_dir=d)
            alpha.info("MARK_ALPHA")
            beta.info("MARK_BETA")

            alpha_path = os.path.join(d, "alpha.log")
            beta_path = os.path.join(d, "beta.log")
            assert os.path.exists(alpha_path), "alpha must log to its own alpha.log"
            assert os.path.exists(beta_path), "beta must log to its own beta.log"

            with open(alpha_path) as f:
                alpha_text = f.read()
            with open(beta_path) as f:
                beta_text = f.read()
            assert "MARK_ALPHA" in alpha_text
            assert "MARK_BETA" in beta_text
            assert "MARK_ALPHA" not in beta_text, "loggers must not cross-contaminate"
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_total_size_bounded_by_rotation(self):
        """A single logger writing far past its cap stays bounded on disk."""
        d = tempfile.mkdtemp()
        try:
            lg = LuminaLogger("bounded", log_dir=d,
                              max_file_size=10 * 1024, max_total_size=40 * 1024)
            payload = "y" * 400
            for _ in range(3000):  # ~1.2 MB of writes vs a 40 KB cap
                lg.info(payload)

            files = [f for f in os.listdir(d) if f.startswith("bounded.log")]
            total = sum(os.path.getsize(os.path.join(d, f)) for f in files)
            cap = 10 * 1024 * (40 // 10)  # max_file_size * (backups + 1) == max_total_size
            assert total <= cap * 1.5, f"total {total} bytes should stay near the {cap} cap"
            assert len(files) <= (40 // 10) + 1, f"too many files: {files}"
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_concurrent_loggers_do_not_delete_each_others_files(self):
        """One logger's rotation must never remove another logger's files."""
        d = tempfile.mkdtemp()
        try:
            keeper = LuminaLogger("keeper", log_dir=d,
                                  max_file_size=10 * 1024, max_total_size=40 * 1024)
            spammer = LuminaLogger("spammer", log_dir=d,
                                   max_file_size=10 * 1024, max_total_size=40 * 1024)
            keeper.info("KEEPER_PRESENT")

            payload = "z" * 400
            for _ in range(3000):  # spammer floods well past the old global cap
                spammer.info(payload)

            keeper_path = os.path.join(d, "keeper.log")
            assert os.path.exists(keeper_path), "spammer must not delete keeper's log"
            with open(keeper_path) as f:
                assert "KEEPER_PRESENT" in f.read()
        finally:
            shutil.rmtree(d, ignore_errors=True)

