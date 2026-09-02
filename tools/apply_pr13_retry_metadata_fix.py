from pathlib import Path

path = Path("app/engine.py")
text = path.read_text()

old = '''    def _finalize_signal_path(self, watch, incomplete_reason=None):
        """Persist remaining rows, summary and the durable finalized marker.

        One transaction.  The caller releases the watch only when this returns
        True, so a failed write always leaves an owner to retry.
        """
        try:
'''
new = '''    def _finalize_signal_path(self, watch, incomplete_reason=None):
        """Persist remaining rows, summary and the durable finalized marker.

        One transaction.  The caller releases the watch only when this returns
        True, so a failed write always leaves an owner to retry.  Recovery
        metadata belongs to the watch owner as well: a failed first attempt
        must not lose the reason when the same watch retries later.
        """
        if incomplete_reason is None:
            incomplete_reason = watch.get("incomplete_reason")
        try:
'''
if text.count(old) != 1:
    raise RuntimeError("could not locate _finalize_signal_path guard")
text = text.replace(old, new, 1)

old = '''                "market": row.get("market"), "event": row.get("event"),
                "expires_at": 0.0, "retry_only": True,
            }
            if self._finalize_signal_path(
                watch, incomplete_reason="in_memory_tail_lost_on_restart",
            ):
'''
new = '''                "market": row.get("market"), "event": row.get("event"),
                "expires_at": 0.0, "retry_only": True,
                "incomplete_reason": "in_memory_tail_lost_on_restart",
            }
            if self._finalize_signal_path(watch):
'''
if text.count(old) != 1:
    raise RuntimeError("could not locate restart-watch construction")
text = text.replace(old, new, 1)
path.write_text(text)

# One-shot helper: leave no maintenance machinery in the PR.
Path("tools/apply_pr13_retry_metadata_fix.py").unlink()
Path(".github/workflows/apply-pr13-retry-metadata-fix.yml").unlink()
