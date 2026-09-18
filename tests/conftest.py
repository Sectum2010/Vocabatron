import socket

import pytest


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def reject(*args,**kwargs):
        raise AssertionError("Default public tests must not access the network or Ollama")
    monkeypatch.setattr(socket.socket,"connect",reject)
    monkeypatch.setattr(socket,"create_connection",reject)
