"""
LuminaLogger: per-logger rotating file logs with a bounded on-disk footprint.

Each logger instance writes to its OWN name-scoped file (``<name>.log``) via the
standard-library ``RotatingFileHandler``. Because filenames are scoped by logger
name and rotation is atomic, multiple instances sharing a directory never collide
on a shared file or delete each other's still-open logs — which previously let
``lumina-modbus-server`` leak gigabytes of deleted-but-open logs and fill the eMMC.

Features:
- Per-instance, name-scoped log file with automatic size-based rotation
- Bounded total size: ``max_file_size`` x (backups + 1) == ``max_total_size``
- Console + file output with consistent formatting
- Standard logging levels (DEBUG, INFO, WARNING, ERROR, CRITICAL)
"""

import os
import re
import logging
from logging.handlers import RotatingFileHandler


class LuminaLogger:
    """A logging utility that keeps each logger's on-disk footprint bounded.

    Attributes:
        name (str): Logger identifier name.
        log_dir (str): Directory path for log files.
        logger (logging.Logger): Underlying Python logger.
        max_file_size (int): Max size of each log file in bytes (rotation threshold).
        max_total_size (int): Max total size of this logger's files in bytes.
        backup_count (int): Number of rotated backups kept (derived from the sizes).
        current_log_file (RotatingFileHandler): The active rotating file handler.
    """

    def __init__(self, name, log_dir='logs',
                 max_file_size=5 * 1024 * 1024,
                 max_total_size=20 * 1024 * 1024):
        """Initialize the logger.

        Args:
            name (str): Name identifier for the logger; also the log file stem.
            log_dir (str): Directory (relative to this module) for log files.
            max_file_size (int): Per-file rotation threshold in bytes.
            max_total_size (int): Total budget in bytes for this logger's files.
        """
        self.name = name
        self.log_dir = os.path.join(os.path.dirname(__file__), log_dir)
        self.max_file_size = max_file_size
        self.max_total_size = max_total_size
        # Total budget = max_file_size * (backup_count + 1); always keep >= 1 backup.
        self.backup_count = max(1, (max_total_size // max_file_size) - 1)

        os.makedirs(self.log_dir, exist_ok=True)

        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        # Re-init safely: close and drop any handlers left by a prior instance of
        # this same name so we never leak file descriptors or double-log.
        for handler in list(self.logger.handlers):
            try:
                handler.close()
            except Exception:
                pass
            self.logger.removeHandler(handler)
        # Self-contained: don't propagate to the root logger (avoids double output).
        self.logger.propagate = False

        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s')

        # One name-scoped rotating file per logger instance. RotatingFileHandler
        # renames <name>.log -> <name>.log.1 ... atomically and deletes the oldest
        # backup, so the footprint is hard-capped and nothing is held open after
        # deletion.
        file_handler = RotatingFileHandler(
            os.path.join(self.log_dir, f'{self._safe_name(name)}.log'),
            maxBytes=max_file_size,
            backupCount=self.backup_count,
            encoding='utf-8',
            delay=True,
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)
        self.current_log_file = file_handler

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)

    @staticmethod
    def _safe_name(name):
        """Return a filesystem-safe stem for the log file name."""
        return re.sub(r'[^A-Za-z0-9._-]', '_', str(name)) or 'lumina'

    def get_total_log_size(self):
        """Return the total size in bytes of this logger's files (base + backups)."""
        base = os.path.join(self.log_dir, f'{self._safe_name(self.name)}.log')
        candidates = [base] + [f'{base}.{i}' for i in range(1, self.backup_count + 1)]
        total = 0
        for path in candidates:
            try:
                total += os.path.getsize(path)
            except OSError:
                pass  # Not yet created or already rotated away.
        return total

    def debug(self, message):
        """Log a debug-level message."""
        self.logger.debug(message)

    def info(self, message):
        """Log an info-level message."""
        self.logger.info(message)

    def warning(self, message):
        """Log a warning-level message."""
        self.logger.warning(message)

    def error(self, message, exc_info=None):
        """Log an error-level message, optionally with exception info."""
        self.logger.error(message, exc_info=exc_info)

    def critical(self, message):
        """Log a critical-level message."""
        self.logger.critical(message)
