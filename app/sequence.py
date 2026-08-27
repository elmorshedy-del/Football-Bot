"""Kalshi WebSocket sequence tracking at subscription scope."""


class SubscriptionSequenceTracker:
    """Sequence validation keyed by Kalshi subscription id (``sid``)."""

    def __init__(self):
        self._last = {}

    def track(self, sid, seq):
        """Return ``ok``, ``duplicate``, or ``gap`` for one sequenced frame."""
        if not isinstance(sid, int) or not isinstance(seq, int):
            return "ok"
        last = self._last.get(sid)
        if last is None:
            self._last[sid] = seq
            return "ok"
        if seq == last + 1:
            self._last[sid] = seq
            return "ok"
        if seq == last:
            return "duplicate"
        self._last[sid] = seq
        return "gap"

    def reset(self, sid=None):
        if sid is None:
            self._last.clear()
        else:
            self._last.pop(sid, None)

    def last(self, sid):
        return self._last.get(sid)
