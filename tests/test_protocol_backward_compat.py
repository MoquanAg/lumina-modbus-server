"""
Tests for backward-compatibility of protocol message format (6 vs 7 fields).

This test suite verifies that the LuminaModbusServer accepts both:
- 6-field protocol (no timeout, uses default 5.0s): command_id:device_type:port:baudrate:hex:length
- 7-field protocol (with timeout): command_id:device_type:port:baudrate:hex:length:timeout
"""

import pytest
import queue
from unittest.mock import MagicMock, patch, mock_open
import threading


@pytest.fixture
def mock_server():
    """Create a mock server instance for testing protocol parsing."""
    # Mock the server class without initializing full state
    server = MagicMock()
    server.logger = MagicMock()
    server.command_queues = {
        '/dev/ttyAMA0': queue.Queue(maxsize=100),
        '/dev/ttyAMA1': queue.Queue(maxsize=100),
        '/dev/ttyAMA2': queue.Queue(maxsize=100),
        '/dev/ttyAMA3': queue.Queue(maxsize=100),
    }
    server.client_pending_commands = {1: set()}
    server.clients = {1: MagicMock()}

    # Import the actual method from main.py
    from main import LuminaModbusServer
    # Bind the real method to our mock
    server.process_client_message = LuminaModbusServer.process_client_message.__get__(server)
    server.send_error_sync = MagicMock()

    return server


class TestProtocolBackwardCompatibility:
    """Test backward-compatibility of 6-field and 7-field protocol formats."""

    def test_7_field_protocol_with_timeout(self, mock_server):
        """Test that 7-field protocol (with timeout) works correctly."""
        client_id = 1
        client_socket = MagicMock()
        # Format: command_id:device_type:port:baudrate:hex:length:timeout
        message = "test_001:THC:/dev/ttyAMA0:9600:0103000000000484:8:3.0"

        mock_server.process_client_message(client_id, message, client_socket)

        # Verify command was queued
        assert not mock_server.command_queues['/dev/ttyAMA0'].empty()
        command_info = mock_server.command_queues['/dev/ttyAMA0'].get_nowait()

        assert command_info['command_id'] == 'test_001'
        assert command_info['device_type'] == 'THC'
        assert command_info['timeout'] == 3.0
        assert command_info['response_length'] == 8
        assert mock_server.send_error_sync.call_count == 0

    def test_6_field_protocol_without_timeout(self, mock_server):
        """Test that 6-field protocol (without timeout, using default) works correctly."""
        client_id = 1
        client_socket = MagicMock()
        # Format: command_id:device_type:port:baudrate:hex:length (no timeout field)
        message = "test_002:EC:/dev/ttyAMA1:9600:0103000000000484:8"

        mock_server.process_client_message(client_id, message, client_socket)

        # Verify command was queued
        assert not mock_server.command_queues['/dev/ttyAMA1'].empty()
        command_info = mock_server.command_queues['/dev/ttyAMA1'].get_nowait()

        assert command_info['command_id'] == 'test_002'
        assert command_info['device_type'] == 'EC'
        # Should use default timeout of 5.0
        assert command_info['timeout'] == 5.0
        assert command_info['response_length'] == 8
        assert mock_server.send_error_sync.call_count == 0

    def test_6_field_protocol_various_baudrates(self, mock_server):
        """Test 6-field protocol with various baudrates."""
        client_id = 1
        client_socket = MagicMock()

        baudrates = ['9600', '19200', '38400', '115200']
        for baudrate in baudrates:
            message = f"test_{baudrate}:DO:/dev/ttyAMA2:{baudrate}:0103000000000484:8"
            mock_server.process_client_message(client_id, message, client_socket)

            command_info = mock_server.command_queues['/dev/ttyAMA2'].get_nowait()
            assert command_info['baudrate'] == int(baudrate)
            assert command_info['timeout'] == 5.0

    def test_invalid_6_field_protocol_too_short(self, mock_server):
        """Test that protocol with fewer than 6 fields is rejected."""
        client_id = 1
        client_socket = MagicMock()
        # Only 5 fields - should be rejected
        message = "test_003:THC:/dev/ttyAMA0:9600:0103000000000484"

        mock_server.process_client_message(client_id, message, client_socket)

        # Should not queue
        assert mock_server.command_queues['/dev/ttyAMA0'].empty()
        mock_server.logger.error.assert_called()

    def test_invalid_timeout_value(self, mock_server):
        """Test that invalid timeout value is handled gracefully."""
        client_id = 1
        client_socket = MagicMock()
        # Timeout field is not a valid float
        message = "test_004:THC:/dev/ttyAMA0:9600:0103000000000484:8:invalid_timeout"

        mock_server.process_client_message(client_id, message, client_socket)

        # Should not queue
        assert mock_server.command_queues['/dev/ttyAMA0'].empty()
        # Should call send_error_sync
        mock_server.send_error_sync.assert_called_once()

    def test_timeout_default_value_5_seconds(self, mock_server):
        """Test that the default timeout is specifically 5.0 seconds."""
        client_id = 1
        client_socket = MagicMock()
        message = "test_006:Solar:/dev/ttyAMA0:9600:0103000000000484:10"

        mock_server.process_client_message(client_id, message, client_socket)

        command_info = mock_server.command_queues['/dev/ttyAMA0'].get_nowait()
        assert command_info['timeout'] == 5.0

    def test_client_pending_commands_updated(self, mock_server):
        """Test that client_pending_commands set is updated correctly."""
        client_id = 1
        client_socket = MagicMock()
        message = "test_007:NPK:/dev/ttyAMA0:9600:0103000000000484:6"

        mock_server.process_client_message(client_id, message, client_socket)

        assert 'test_007' in mock_server.client_pending_commands[client_id]

    def test_hex_command_parsing(self, mock_server):
        """Test that hex command is correctly parsed."""
        client_id = 1
        client_socket = MagicMock()
        hex_cmd = "0103000000000484"
        message = f"test_008:THC:/dev/ttyAMA0:9600:{hex_cmd}:8"

        mock_server.process_client_message(client_id, message, client_socket)

        command_info = mock_server.command_queues['/dev/ttyAMA0'].get_nowait()
        expected_bytes = bytes.fromhex(hex_cmd)
        assert command_info['command'] == expected_bytes


class TestProtocolEdgeCases:
    """Test edge cases and boundary conditions for protocol parsing."""

    def test_7_field_with_zero_timeout(self, mock_server):
        """Test 7-field protocol with zero timeout."""
        client_id = 1
        client_socket = MagicMock()
        message = "test_009:THC:/dev/ttyAMA0:9600:0103000000000484:8:0.0"

        mock_server.process_client_message(client_id, message, client_socket)

        command_info = mock_server.command_queues['/dev/ttyAMA0'].get_nowait()
        assert command_info['timeout'] == 0.0

    def test_7_field_with_large_timeout(self, mock_server):
        """Test 7-field protocol with large timeout value."""
        client_id = 1
        client_socket = MagicMock()
        message = "test_010:THC:/dev/ttyAMA0:9600:0103000000000484:8:300.0"

        mock_server.process_client_message(client_id, message, client_socket)

        command_info = mock_server.command_queues['/dev/ttyAMA0'].get_nowait()
        assert command_info['timeout'] == 300.0

    def test_response_length_zero(self, mock_server):
        """Test protocol with response_length of 0."""
        client_id = 1
        client_socket = MagicMock()
        message = "test_011:Relay:/dev/ttyAMA0:9600:010500010000:0"

        mock_server.process_client_message(client_id, message, client_socket)

        command_info = mock_server.command_queues['/dev/ttyAMA0'].get_nowait()
        assert command_info['response_length'] == 0

    def test_extra_fields_beyond_7(self, mock_server):
        """Test that protocol only reads first 6 or 7 fields, ignoring extras."""
        client_id = 1
        client_socket = MagicMock()
        # 8+ fields - should still work, extra fields ignored
        message = "test_012:THC:/dev/ttyAMA0:9600:0103000000000484:8:2.0:extra:field:data"

        mock_server.process_client_message(client_id, message, client_socket)

        command_info = mock_server.command_queues['/dev/ttyAMA0'].get_nowait()
        assert command_info['timeout'] == 2.0
        # Extra fields should be ignored
        assert command_info['device_type'] == 'THC'
