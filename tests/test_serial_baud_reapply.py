"""A cached descriptor's baud is not necessarily the physical UART's baud."""

import os
import pty
import termios
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
import serial

from main import LuminaModbusServer, SerialConnection


REQUEST = bytes.fromhex('4103000100041b09')
RESPONSE = bytes.fromhex('4103080000000200000000b9d6')
TIMEOUT = 0.25


@contextmanager
def shared_uart(creation_order):
    master, slave = pty.openpty()
    port = os.ttyname(slave)
    handles = {}
    server = object.__new__(LuminaModbusServer)
    server.logger = Mock()
    server.port_loggers = {port: Mock()}
    server.serial_connections = {port: {}}
    try:
        for baud in creation_order:
            handle = serial.Serial(port, baudrate=baud, timeout=TIMEOUT,
                                   write_timeout=TIMEOUT)
            handles[baud] = handle
            server.serial_connections[port][baud] = SerialConnection(
                handle, port, baud, last_used=0, min_command_spacing=0)
        yield server, port, slave, handles
    finally:
        for handle in handles.values():
            handle.close()
        os.close(master)
        os.close(slave)


def transaction(server, port, baud):
    return server.execute_serial_command(port, {
        'baudrate': baud, 'command': REQUEST,
        'timeout': TIMEOUT, 'response_length': len(RESPONSE),
    })


def emulate_response(monkeypatch, handle, slave, expected_baud):
    pending = bytearray()
    writes = []

    def write(data):
        # Query shared OS state at transmission, not the object's cached value.
        attrs = termios.tcgetattr(slave)
        expected = getattr(termios, f'B{expected_baud}')
        assert attrs[4:6] == [expected, expected]
        assert data == REQUEST
        writes.append(data)
        pending.extend(RESPONSE)
        return len(data)

    def read(size):
        result = bytes(pending[:size])
        del pending[:size]
        return result

    monkeypatch.setattr(handle, 'write', write)
    monkeypatch.setattr(handle, 'read', read)
    return writes


@pytest.mark.parametrize('order', [(9600, 38400), (38400, 9600)])
def test_cached_handles_restore_actual_uart_baud_at_each_write(monkeypatch, order):
    monkeypatch.setattr('main.time.sleep', lambda seconds: None)
    with shared_uart(order) as (server, port, slave, handles):
        writes = {baud: emulate_response(monkeypatch, handle, slave, baud)
                  for baud, handle in handles.items()}
        # Equal timeouts ensure the cache lookup cannot accidentally fix baud.
        for baud in (order[0], order[1], order[0], order[0], order[1]):
            assert transaction(server, port, baud) == RESPONSE
            assert handles[baud].timeout == TIMEOUT
            assert handles[baud].write_timeout == TIMEOUT
        assert sum(map(len, writes.values())) == 5


def test_baud_reconfigure_failure_prevents_flush_and_transmission(monkeypatch):
    with shared_uart((9600,)) as (server, port, slave, handles):
        handle = handles[9600]
        reset_input, reset_output, write = Mock(), Mock(), Mock()
        monkeypatch.setattr(handle, 'reset_input_buffer', reset_input)
        monkeypatch.setattr(handle, 'reset_output_buffer', reset_output)
        monkeypatch.setattr(handle, 'write', write)
        monkeypatch.setattr(handle, '_reconfigure_port',
                            Mock(side_effect=serial.SerialException('configuration failed')))
        with pytest.raises(serial.SerialException, match='configuration failed'):
            transaction(server, port, 9600)
        reset_input.assert_not_called()
        reset_output.assert_not_called()
        write.assert_not_called()


def test_reapplying_one_uart_does_not_change_another(monkeypatch):
    monkeypatch.setattr('main.time.sleep', lambda seconds: None)
    with shared_uart((9600, 38400)) as (server, port, slave, handles):
        with shared_uart((19200,)) as (_, other_port, other_slave, other_handles):
            before = termios.tcgetattr(other_slave)
            emulate_response(monkeypatch, handles[9600], slave, 9600)
            assert transaction(server, port, 9600) == RESPONSE
            assert termios.tcgetattr(other_slave) == before
            assert other_handles[19200].baudrate == 19200
