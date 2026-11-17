"""Security events for a SIEM, sent to Splunk's HTTP Event Collector (HEC).

Two kinds of event, both in one index and joined on conversation_id:

  helpdesk:audit  one per tool call, written by the server as it decides
                  (lookup, verification attempt, lockout, unlock). The facts.
  helpdesk:call   one per finished call, built from ElevenLabs' post-call
                  webhook (see calls.py). The context.

Events are sent from a background thread so a slow or unreachable Splunk never
delays the caller. If HEC cannot be reached or refuses an event, the event is
appended to a local JSON-lines spool file instead of being lost; in a real
deployment a Universal Forwarder would pick that file up.

The HEC token only allows writing, and only to the index chosen in Splunk.
TLS is verified against a CA file; there is no option to skip verification.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Protocol

import httpx

log = logging.getLogger("helpdesk.events")

SOURCE = "mande-voice-agent"


class EventSink(Protocol):
    def send(self, sourcetype: str, event: dict, at: float | None = None) -> None: ...


class MemorySink:
    """Keeps events in a list. For tests."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def send(self, sourcetype: str, event: dict, at: float | None = None) -> None:
        self.events.append({"sourcetype": sourcetype, "time": at, "event": event})

    def of(self, sourcetype: str) -> list[dict]:
        return [e["event"] for e in self.events if e["sourcetype"] == sourcetype]


def _envelope(sourcetype: str, event: dict, at: float | None) -> dict:
    return {"time": round(at if at is not None else time.time(), 3), "source": SOURCE,
            "sourcetype": sourcetype, "event": event}


class FileSink:
    """Appends HEC-shaped events to a JSON-lines file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, envelopes: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            for e in envelopes:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def send(self, sourcetype: str, event: dict, at: float | None = None) -> None:
        self.write([_envelope(sourcetype, event, at)])


class HecSink:
    def __init__(
        self,
        url: str,
        token: str,
        ca_path: Path | str,
        spool: FileSink,
        transport: httpx.BaseTransport | None = None,
        background: bool = True,
        batch_size: int = 50,
    ) -> None:
        self._endpoint = url.rstrip("/") + "/services/collector/event"
        self._http = httpx.Client(
            headers={"Authorization": f"Splunk {token}"},
            verify=str(ca_path) if transport is None else True,
            timeout=5.0,
            transport=transport,
        )
        self.spool = spool
        self._batch_size = batch_size
        self._queue: queue.Queue[dict] | None = None
        if background:
            self._queue = queue.Queue(maxsize=10_000)
            threading.Thread(target=self._run, name="hec-sender", daemon=True).start()

    def send(self, sourcetype: str, event: dict, at: float | None = None) -> None:
        envelope = _envelope(sourcetype, event, at)
        if self._queue is None:
            self._post([envelope])
            return
        try:
            self._queue.put_nowait(envelope)
        except queue.Full:
            self.spool.write([envelope])

    def flush(self, timeout: float = 10.0) -> None:
        """Wait until queued events are sent (or spooled)."""
        if self._queue is None:
            return
        deadline = time.time() + timeout
        while self._queue.unfinished_tasks and time.time() < deadline:
            time.sleep(0.05)

    def _run(self) -> None:
        assert self._queue is not None
        while True:
            batch = [self._queue.get()]
            while len(batch) < self._batch_size:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._post(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _post(self, envelopes: list[dict]) -> None:
        # HEC accepts several events in one request, concatenated.
        body = "".join(json.dumps(e, ensure_ascii=False) for e in envelopes)
        try:
            r = self._http.post(self._endpoint, content=body.encode("utf-8"),
                                headers={"Content-Type": "application/json"})
            if r.status_code == 200:
                return
            log.warning("HEC refused %d event(s): %s %s", len(envelopes), r.status_code, r.text[:200])
        except httpx.HTTPError as e:
            log.warning("HEC unreachable, spooling %d event(s): %s", len(envelopes), e)
        self.spool.write(envelopes)


def build_sink(settings) -> EventSink:
    """HEC when configured, otherwise only the local JSON-lines file."""
    spool = FileSink(settings.events_spool_path)
    if settings.splunk_hec_url and settings.splunk_hec_token:
        if not Path(settings.splunk_ca_path).is_file():
            log.error("Splunk CA file %s not found; writing events to %s only",
                      settings.splunk_ca_path, settings.events_spool_path)
            return spool
        return HecSink(settings.splunk_hec_url, settings.splunk_hec_token, settings.splunk_ca_path, spool)
    return spool
