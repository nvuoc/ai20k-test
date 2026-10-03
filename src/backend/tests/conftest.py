"""The test suite must never connect to live providers or read local secrets."""

import socket

import pytest


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch):
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create = socket.create_connection

    def check(address):
        # Windows asyncio uses a loopback socket pair for its own event loop.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1", "localhost"}:
            return
        raise AssertionError("Outbound network access is forbidden in tests; use MockTransport")

    def connect(sock, address):
        check(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address)
        return original_connect_ex(sock, address)

    def create(address, *args, **kwargs):
        check(address)
        return original_create(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create)
    for name in ("GEMINI_API_KEY", "VIETMAP_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
