"""Busy-port requests survive queueing without extending serial I/O deadlines."""
import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from main import LuminaModbusServer, SerialConnection


PORT = '/dev/ttyAMA2'
ON = bytes.fromhex('4106000100011e0a')
OFF_BODY = bytes.fromhex('410600010000')


@pytest.fixture
def rig(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr('main.time.monotonic', lambda: clock.now)
    monkeypatch.setattr('main.time.time', lambda: clock.now)
    monkeypatch.setattr('main.time.sleep', lambda value: setattr(clock, 'now', clock.now + value))
    server = object.__new__(LuminaModbusServer)
    server.logger = Mock()
    server.port_loggers = {PORT: Mock()}
    server.command_queues = {PORT: queue.Queue()}
    server.client_pending_commands = {1: set()}
    server.clients = {1: Mock()}
    server.send_error_sync = Mock()
    server.send_response_sync = Mock()
    server.shutdown_event = threading.Event()
    server.port_last_activity = {}
    serial = Mock(timeout=0.5, write_timeout=0.5)
    serial.timeout, serial.write_timeout = 0.5, 0.5
    conn = SerialConnection(serial, PORT, 9600, last_used=0, min_command_spacing=0)
    server.get_serial_connection = Mock(return_value=conn)
    return server, serial, conn, clock


def queued(server, suffix=':0.5:6'):
    command = OFF_BODY + server._calculate_crc(OFF_BODY)
    server.process_client_message(1, f'cid:write_fresh_air:{PORT}:9600:{command.hex()}:8{suffix}', Mock())
    return server.command_queues[PORT].get_nowait()


def respond(serial, data):
    pending = bytearray(data)
    def read(size):
        value = bytes(pending[:size]); del pending[:size]
        return value
    serial.read.side_effect = read


def test_off_after_four_second_relay_backlog_executes_once(rig):
    server, serial, _, clock = rig
    info = queued(server)
    clock.now += 4
    respond(serial, info['command'])
    assert server.execute_serial_command(PORT, info) == info['command']
    serial.write.assert_called_once_with(info['command'])
    assert serial.timeout == serial.write_timeout == 0.5


@pytest.mark.parametrize('delay', [6, 7])
def test_expired_queued_on_never_transmits(rig, delay):
    server, serial, _, clock = rig
    info = queued(server); info['command'] = ON
    clock.now += delay
    with pytest.raises(TimeoutError, match='queue_expired'):
        server.execute_serial_command(PORT, info)
    serial.write.assert_not_called()


def test_expiry_during_spacing_never_transmits(rig):
    server, serial, conn, clock = rig
    info = queued(server); clock.now += 5.99
    conn.last_used, conn.min_command_spacing = clock.now, 0.2
    with pytest.raises(TimeoutError, match='queue_expired'):
        server.execute_serial_command(PORT, info)
    serial.write.assert_not_called()


def test_initial_read_cannot_double_response_timeout(rig):
    server, serial, _, clock = rig
    info = queued(server)
    def silence(_):
        clock.now += serial.timeout
        return b''
    serial.read.side_effect = silence
    with pytest.raises(TimeoutError, match='serial_response_timeout'):
        server.execute_serial_command(PORT, info)
    assert clock.now == 100.5
    assert serial.timeout == serial.write_timeout == 0.5
    serial.write.assert_called_once()


def test_contamination_is_terminal_and_does_not_replay_write(rig):
    server, serial, _, _ = rig
    info = queued(server); respond(serial, b'\x10\x06\x00')
    with pytest.raises(ValueError, match='Unexpected slave'):
        server.execute_serial_command(PORT, info)
    serial.write.assert_called_once()


def test_queue_expiry_returns_terminal_error_from_worker(rig):
    server, serial, _, clock = rig
    info = queued(server); clock.now += 7
    server.command_queues[PORT].task_done()
    server.command_queues[PORT].put(info)
    server.send_error_sync.side_effect = lambda *_: server.shutdown_event.set()
    server.process_serial_port(PORT)
    server.send_error_sync.assert_called_once_with(info, 'queue_expired')
    serial.write.assert_not_called()
    assert server.command_queues[PORT].unfinished_tasks == 0


def test_crc_valid_wrong_byte_count_is_not_a_status_read(rig):
    server, serial, _, _ = rig
    info = queued(server)
    body = bytes.fromhex('410300010004')
    info['command'] = body + server._calculate_crc(body)
    info['response_length'] = 13
    body = bytes.fromhex('4103060000000200000000')
    respond(serial, body + server._calculate_crc(body))
    with pytest.raises(ValueError, match='byte count'):
        server.execute_serial_command(PORT, info)


@pytest.mark.parametrize('echo', [False, True])
def test_valid_response_with_request_header_is_not_mistaken_for_echo(rig, echo):
    server, serial, _, _ = rig
    info = queued(server)
    info['command'] = bytes.fromhex('4103020000018b72')
    info['response_length'] = 7
    response = bytes.fromhex('4103020050b9b7')
    respond(serial, (info['command'] if echo else b'') + response)
    assert server.execute_serial_command(PORT, info) == response


@pytest.mark.parametrize('budget', ['nan', 'inf', '-1', '0', '31', 'bad'])
def test_bad_budget_is_rejected_before_queueing(rig, budget):
    server, serial, _, _ = rig
    server.process_client_message(1, f'bad:x:{PORT}:9600:{ON.hex()}:8:0.5:{budget}', Mock())
    assert server.command_queues[PORT].empty()
    server.send_error_sync.assert_called_once()
    serial.write.assert_not_called()
