"""Raw WebSocket feed recorder — the research goldmine.

Every message is appended (with local wall + monotonic stamps) to hourly
gzip JSONL segments under DATA_DIR/raw/. This is what later replaces the
print-constrained fill model with true book-at-arrival replays."""
import gzip
import json
import os
import time

from . import config


class RawRecorder:
    def __init__(self, on_error=None):
        self.dir = os.path.join(config.DATA_DIR, "raw")
        self.on_error = on_error
        self._fh = None
        self._hour = None
        self._n_since_flush = 0
        self._last_alert_ts = 0.0
        self.total = 0
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

    def _rotate(self):
        hour = time.strftime("%Y%m%d-%H", time.gmtime())
        if hour != self._hour:
            if self._fh:
                self._fh.close()
            self._hour = hour
            self._fh = gzip.open(os.path.join(self.dir, f"feed-{hour}.jsonl.gz"), "at")

    def write(self, msg, local_wall, local_mono):
        try:
            self._rotate()
            self._fh.write(json.dumps({"lt": round(local_wall, 6),
                                       "lm": round(local_mono, 6), "m": msg},
                                      separators=(",", ":")) + "\n")
            self.total += 1
            self.last_write_ts = local_wall
            self.healthy = True
            self._n_since_flush += 1
            if self._n_since_flush >= 200:
                self._fh.flush()
                self._n_since_flush = 0
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

    def status(self):
        return {
            "healthy": self.healthy,
            "failures": self.failures,
            "last_error": self.last_error,
            "last_write_ts": self.last_write_ts,
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
        """Finalize and rotate the active segment before an export snapshot."""
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
        return finalized_path

    def close(self):
        self.checkpoint()
