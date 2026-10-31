"""Shared fixtures.

The test suite must never reach Cyberwave, Sarvam or any other remote host:
an autouse fixture makes every non-loopback socket connection fail loudly.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skypaw.app import create_app
from skypaw.config import Settings

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


class NetworkBlocked(RuntimeError):
    pass


def _is_local(address: object) -> bool:
    if isinstance(address, str):  # AF_UNIX path
        return True
    if isinstance(address, tuple) and address:
        host = str(address[0])
        if host in {"localhost", ""}:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
    return False


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def guarded_connect(self: socket.socket, address: object) -> None:
        if not _is_local(address):
            raise NetworkBlocked(f"tests must not open network connections (to {address!r})")
        return _real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: object) -> int:
        if not _is_local(address):
            raise NetworkBlocked(f"tests must not open network connections (to {address!r})")
        return _real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings isolated from the developer's environment and .env file."""
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "no-dist",
        skypaw_env="dev",
        git_sha="test",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c
