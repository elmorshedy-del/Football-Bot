"""Raw WebSocket feed recorder — the research goldmine.

Every message is appended to hourly gzip JSONL segments under DATA_DIR/raw/.
This is what later replaces the print-constrained fill model with true
book-at-arrival replays.

Frame layout (keys in this order, the optional ones omitted when unknown):

    {"lt": <processing wall>, "lm": <processing monotonic>,
     "at": <arrival wall>,    "am": <arrival monotonic>,   "bl": <backlog>,
     "m": <exchange message>}

``lt``/``lm`` are stamped when the consumer dequeues the frame -- the meaning
they always had, kept for existing readers.  ``at``/``am`` are stamped by the
socket reader the moment the frame is received, and ``bl`` is how many frames
were still queued behind it.  During a backlog ``lt - at`` is the processing
delay a frame suffered, which the old two-stamp layout silently folded into
every timestamp derived from it.

Feed-health events (connect, gap, rotation, ...) are written into the same
stream as ``{"type": "recorder_marker", "kind": ..., "detail": ...}`` frames so
a segment explains its own discontinuities without the database."""
import gzip
import json
import os
import time

from . import config

MARKER_TYPE = "recorder_marker"


class RawRecorder:
    def __init__(self, on_error=None, on_event=None):
        self.dir = os.path.join(config.DATA_DIR, "raw")
        self.on_error = on_error
        # Feed-health ledger callback: (kind, detail, marker=bool).  ``marker``
        # is False when the recorder must not be re-entered to write the
        # matching stream marker (a rotation forced from a worker thread).
        self.on_event = on_event
        self._fh = None
        self._hour = None
        self._n_since_flush = 0
        self._last_alert_ts = 0.0
        self.total = 0
        self.markers = 0
        self.event_failures = 0
        self.failures = 0
        self.healthy = True
        self.last_error = None
        self.last_write_ts = None
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError as exc:
            self.failures = 1
            self.healthy = False
            self.last_error = f"{type(exc).__name__}: {exc}"

    def _emit(self, kind, detail, marker=True):
        """Report a feed-health event; a ledger fault is counted, never raised.

        The recorder is on the WebSocket path, so a failure to describe a
        rotation must not become a failure to record the feed.  The count is
        surfaced in `status()` so the silence is visible.
        """
        if self.on_event is None:
            return
        try:
            self.on_event(kind, detail, marker)
        except Exception:
            self.event_failures += 1

    def _rotate(self):
        hour = time.strftime("%Y%m%d-%H", time.gmtime())
        if hour != self._hour:
            previous = self._hour
            if self._fh:
                self._fh.close()
            self._hour = hour
            self._fh = gzip.open(os.path.join(self.dir, f"feed-{hour}.jsonl.gz"), "at")
            if previous is not None:
                # A real hour boundary, not the first open or a reopen after a
                # checkpoint.  The marker lands in the new segment.
                self._emit("recorder_rotate", {"reason": "hour",
                                               "previous": previous, "hour": hour})

    def _write_frame(self, frame, local_wall, counted=True):
        """Append one line.  `counted` keeps `total` a count of EXCHANGE frames,
        so a marker does not inflate the recorded-frame figure on the status
        panel; markers have their own counter."""
        try:
            self._rotate()
            self._fh.write(json.dumps(frame, separators=(",", ":")) + "\n")
            if counted:
                self.total += 1
            self.last_write_ts = local_wall
            self.healthy = True
            self._n_since_flush += 1
            if self._n_since_flush >= 200:
                self._fh.flush()
                self._n_since_flush = 0
            return True
        except (OSError, TypeError, ValueError) as exc:
            self.failures += 1
            self.healthy = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            try:
                if self._fh:
                    self._fh.close()
            except OSError:
                pass
            self._fh = None
            self._hour = None
            now = time.time()
            if self.on_error is not None and now - self._last_alert_ts >= 60:
                self._last_alert_ts = now
                self.on_error(self.last_error)
            return False

    def write(self, msg, local_wall, local_mono, arrival_wall=None, arrival_mono=None,
              backlog=None):
        """Append one exchange message.

        ``local_*`` are the processing stamps (``lt``/``lm``); ``arrival_*``
        and ``backlog`` are written as ``at``/``am``/``bl`` when given and
        omitted otherwise, so older callers produce the older layout.
        """
        frame = {"lt": round(local_wall, 6), "lm": round(local_mono, 6)}
        if arrival_wall is not None:
            frame["at"] = round(arrival_wall, 6)
        if arrival_mono is not None:
            frame["am"] = round(arrival_mono, 6)
        if backlog is not None:
            frame["bl"] = backlog
        frame["m"] = msg
        return self._write_frame(frame, local_wall)

    def write_marker(self, kind, detail=None, wall=None, mono=None):
        """Append a feed-health marker so the segment is self-describing."""
        wall = time.time() if wall is None else wall
        mono = time.monotonic() if mono is None else mono
        frame = {"lt": round(wall, 6), "lm": round(mono, 6),
                 "m": {"type": MARKER_TYPE, "kind": kind, "detail": detail}}
        written = self._write_frame(frame, wall, counted=False)
        if written:
            self.markers += 1
        return written

    def status(self):
        return {
            "healthy": self.healthy,
            "failures": self.failures,
            "last_error": self.last_error,
            "last_write_ts": self.last_write_ts,
            "markers": self.markers,
            "event_failures": self.event_failures,
        }

    def checkpoint(self):
        """Close the active gzip member so an export can copy a valid segment.

        The next message transparently reopens the hourly file in append mode,
        producing a standards-compliant concatenated gzip stream.
        """
        if self._fh:
            self._fh.close()
        self._fh = None
        self._hour = None
        self._n_since_flush = 0

    def checkpoint_for_export(self):
        """Finalize and rotate the active segment before an export snapshot.

        Runs on the export worker thread, so the ledger event is recorded
        without a stream marker: writing into the gzip handle from a second
        thread would race the collector.
        """
        active_hour = self._hour
        self.checkpoint()
        if not active_hour:
            return None
        active_path = os.path.join(self.dir, f"feed-{active_hour}.jsonl.gz")
        if not os.path.isfile(active_path):
            return None
        finalized_path = os.path.join(
            self.dir, f"feed-{active_hour}-part-{time.time_ns()}.jsonl.gz",
        )
        os.replace(active_path, finalized_path)
        self._emit("recorder_rotate", {"reason": "export", "hour": active_hour,
                                       "finalized": os.path.basename(finalized_path)},
                   marker=False)
        return finalized_path

    def close(self):
        self.checkpoint()
