"""Shared test setup: the suite never touches the network or the developer's own .env."""
from __future__ import annotations

import httpx
import pytest


def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no network in tests", request=request)


@pytest.fixture(autouse=True)
def _no_real_probe(monkeypatch):
    """A local-server probe that no test mocked behaves as "server not answering", with no socket opened.
    Tests that need an answering server replace the same function with an httpx.MockTransport."""
    monkeypatch.setattr("trioditis.localcheck._client",
                        lambda **kw: httpx.Client(**{**kw, "transport": httpx.MockTransport(_refused)}))


@pytest.fixture(autouse=True)
def _no_real_env_file(monkeypatch, tmp_path):
    """The CLI loads .env from the working directory into os.environ, where it would stay for the
    rest of the run (a developer's router key then turns later requests into 401s). Point it at a
    file that does not exist; a test of the .env loader sets TRIODITIS_ENV_FILE itself."""
    monkeypatch.setenv("TRIODITIS_ENV_FILE", str(tmp_path / "no-such.env"))
