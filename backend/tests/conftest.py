import os
import socket

import pytest

from wb.config import get_paths

NO_ENV = os.path.join(os.path.dirname(__file__), "data", "no-such.env")


@pytest.fixture(autouse=True)
def no_real_secrets(request, monkeypatch):
    """A unit test never sees this machine's keys (.env, keychain), so it can never reach a real provider, model or
    Feishu bot; a key a test needs is set by the test itself. Live tests (-m live) use the real ones."""
    if request.node.get_closest_marker("live"):
        return
    for k in [k for k in os.environ if k.startswith("WB_SECRET_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("WB_ENV_FILE", NO_ENV)
    kept: dict = {}
    monkeypatch.setattr("keyring.get_password", lambda service, name: kept.get((service, name)))
    monkeypatch.setattr("keyring.set_password", lambda service, name, value: kept.__setitem__((service, name), value))


LOCAL = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """A unit test never reaches the internet: a provider a test forgot to fake fails loudly instead of quietly
    downloading (a route added to the catalog reaches tests that never asked for it). Live tests (-m live) may."""
    if request.node.get_closest_marker("live"):
        return
    lookup = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if host not in LOCAL:
            raise RuntimeError(f"a unit test tried to reach {host}: fake the provider")
        return lookup(host, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def paths(tmp_path):
    return get_paths("test", tmp_path)
