import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock

import main


def make_server():
    server = main.LuminaModbusServer(host="127.0.0.1", port=9999, request_timeout=1.0)
    server.logger = MagicMock()

    # 确保每个 port 都有 logger（avoid environment KeyError）
    for p in main.AVAILABLE_PORTS:
        server.port_loggers[p] = MagicMock()

    return server


def make_client():
    sock = MagicMock()
    sock.send = MagicMock()
    sock.sendall = MagicMock() 
    return sock


# process_client_message

def test_process_client_message_valid_enqueue():
    server = make_server()
    client_socket = make_client()
    client_id = 123

    server.clients.add(client_id)
    server.client_pending_commands[client_id] = set()

    msg = "cmd1:RTU:/dev/ttyAMA0:9600:010300000002:7:1.0"
    server.process_client_message(client_id, msg, client_socket)

    q = server.command_queues["/dev/ttyAMA0"]
    assert not q.empty()

    item = q.get_nowait()
    assert item["client_id"] == client_id
    assert item["command_id"] == "cmd1"
    assert item["baudrate"] == 9600
    assert item["response_length"] == 7
    assert item["socket"] is client_socket

    assert "cmd1" in server.client_pending_commands[client_id]


def test_process_client_message_invalid_port_sends_error():
    server = make_server()
    client_socket = make_client()
    client_id = 123

    server.clients.add(client_id)
    server.client_pending_commands[client_id] = set()

    msg = "cmd2:RTU:/dev/ttyFAKE:9600:010300000002:7:1.0"
    server.process_client_message(client_id, msg, client_socket)

    client_socket.send.assert_called_once()
    payload = client_socket.send.call_args[0][0].decode(errors="ignore")
    assert "cmd2" in payload
    assert "ERROR" in payload


def test_process_client_message_invalid_hex_sends_error():
    server = make_server()
    client_socket = make_client()
    client_id = 123

    server.clients.add(client_id)
    server.client_pending_commands[client_id] = set()

    msg = "cmd3:RTU:/dev/ttyAMA0:9600:ZZZZ:7:1.0"
    server.process_client_message(client_id, msg, client_socket)

    client_socket.send.assert_called_once()
    payload = client_socket.send.call_args[0][0].decode(errors="ignore")
    assert "cmd3" in payload
    assert "ERROR" in payload


def test_process_client_message_queue_full_sends_queue_full():
    server = make_server()
    client_socket = make_client()
    client_id = 123

    server.clients.add(client_id)
    server.client_pending_commands[client_id] = set()

    q = server.command_queues["/dev/ttyAMA0"]
    while not q.full():
        q.put({"dummy": True})

    msg = "cmd4:RTU:/dev/ttyAMA0:9600:010300000002:7:1.0"
    server.process_client_message(client_id, msg, client_socket)

    client_socket.send.assert_called_once()
    payload = client_socket.send.call_args[0][0].decode(errors="ignore")
    assert "cmd4" in payload
    assert "queue_full" in payload or "ERROR" in payload


# execute_pymodbus_command (async)

def _ensure_conn(server, port, baudrate):
    if port not in server.pymodbus_connections:
        server.pymodbus_connections[port] = {}
    server.pymodbus_connections[port][baudrate] = SimpleNamespace(
        last_used=0.0,
        min_command_spacing=0.0
    )


def test_execute_read_holding_registers_calls_pymodbus():
    server = make_server()
    port = "/dev/ttyAMA0"
    baudrate = 9600
    _ensure_conn(server, port, baudrate)

    fake_client = MagicMock()
    fake_client.read_holding_registers = AsyncMock()

    fake_resp = MagicMock()
    fake_resp.isError.return_value = False
    fake_resp.registers = [1, 2]
    fake_client.read_holding_registers.return_value = fake_resp

    async def fake_get_client(p, b, loop):
        return fake_client

    server.get_pymodbus_client = fake_get_client

    command_info = {
        "client_id": 1,
        "command_id": "cmdRH",
        "baudrate": baudrate,
        "command": bytes.fromhex("010300000002"),
        "socket": make_client(),
        "timeout": 1.0,
        "response_length": 7,
        "device_type": "RTU",
    }

    async def run():
        loop = asyncio.get_running_loop()
        return await server.execute_pymodbus_command(port, command_info, loop)

    result = asyncio.run(run())

    fake_client.read_holding_registers.assert_awaited_once()
    assert isinstance(result, (bytes, bytearray))
    assert len(result) > 0


def test_execute_write_single_register_calls_pymodbus():
    server = make_server()
    port = "/dev/ttyAMA0"
    baudrate = 9600
    _ensure_conn(server, port, baudrate)

    fake_client = MagicMock()
    fake_client.write_register = AsyncMock()

    fake_resp = MagicMock()
    fake_resp.isError.return_value = False
    fake_client.write_register.return_value = fake_resp

    async def fake_get_client(p, b, loop):
        return fake_client

    server.get_pymodbus_client = fake_get_client

    command_info = {
        "client_id": 1,
        "command_id": "cmdWR",
        "baudrate": baudrate,
        "command": bytes.fromhex("01060001000A"),
        "socket": make_client(),
        "timeout": 1.0,
        "response_length": 8,
        "device_type": "RTU",
    }

    async def run():
        loop = asyncio.get_running_loop()
        return await server.execute_pymodbus_command(port, command_info, loop)

    result = asyncio.run(run())

    fake_client.write_register.assert_awaited_once()
    assert isinstance(result, (bytes, bytearray))
    assert len(result) > 0



# send_response_sync / send_error_sync

def test_send_response_sync_sends_and_removes_pending():
    server = make_server()
    client_socket = make_client()
    client_id = 777

    server.clients.add(client_id)
    server.client_pending_commands[client_id] = {"cmdX"}

    command_info = {"client_id": client_id, "command_id": "cmdX", "socket": client_socket}
    server.send_response_sync(command_info, b"\x01\x02")

    client_socket.send.assert_called_once()
    assert "cmdX" not in server.client_pending_commands[client_id]


def test_send_error_sync_sends_and_removes_pending():
    server = make_server()
    client_socket = make_client()
    client_id = 888

    server.clients.add(client_id)
    server.client_pending_commands[client_id] = {"cmdE"}

    command_info = {"client_id": client_id, "command_id": "cmdE", "socket": client_socket}
    server.send_error_sync(command_info, "test_error")

    client_socket.send.assert_called_once()
    payload = client_socket.send.call_args[0][0].decode(errors="ignore")
    assert "cmdE" in payload
    assert "ERROR" in payload
    assert "cmdE" not in server.client_pending_commands[client_id]