"""Regression checks never fall through to configured production API credentials."""
import pytest
import requests


@pytest.fixture(autouse=True)
def deny_external_requests(monkeypatch):
    from law_change_auto.services import diagnostics
    diagnostics.reset()
    def denied(*args, **kwargs):
        raise AssertionError('External network disabled in tests; install a transport fixture explicitly')
    monkeypatch.setattr(requests.sessions.Session, 'request', denied)
    yield
    diagnostics.reset()
