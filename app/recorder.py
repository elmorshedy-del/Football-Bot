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
    def __init__(self):
        self.dir = os.path.join(config.DATA_DIR, "raw")
        os.makedirs(self.dir, exist_ok=True)
        self._fh = None
        self._hour = None
        self._n_since_flush = 0
        self.total = 0

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
            self._n_since_flush += 1
            if self._n_since_flush >= 200:
                self._fh.flush()
                self._n_since_flush = 0
        except Exception:
            pass

    def close(self):
        if self._fh:
            self._fh.close()
