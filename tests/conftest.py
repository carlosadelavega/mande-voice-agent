import pytest

import app.main
from app.events import MemorySink


@pytest.fixture(autouse=True)
def memory_events(monkeypatch):
    """Tests never write to Splunk or to logs/; events go to a list."""
    sink = MemorySink()
    monkeypatch.setattr(app.main, "build_sink", lambda settings: sink)
    return sink
