from pathlib import Path

# Correct the three stale restart-test references to the real transactional
# finalizer used by Engine._finalize_signal_path.
path = Path("tests/test_signal_path_ownership.py")
text = path.read_text()
old = "finalize_signal_path_with_rows"
count = text.count(old)
if count != 3:
    raise RuntimeError(f"expected three stale signal finalizer hooks, found {count}")
path.write_text(text.replace(old, "finalize_signal_path"))

# Browser innerText normalizes these UI labels to uppercase. Keep the
# acceptance checks case-insensitive while still requiring the exact words.
path = Path("tests/test_pr13_browser_followup.py")
text = path.read_text()
old_gate = 'self.assertIn("Clock before minute 88", page.inner_text("#trade-list"))'
new_gate = 'self.assertIn("CLOCK BEFORE MINUTE 88", page.inner_text("#trade-list").upper())'
if text.count(old_gate) != 1:
    raise RuntimeError("expected one stale gate-label assertion")
text = text.replace(old_gate, new_gate, 1)
old_mobile = 'self.assertIn(value, text, f"{value!r} missing from 360px {tab} tab")'
new_mobile = 'self.assertIn(value.upper(), text.upper(), f"{value!r} missing from 360px {tab} tab")'
if text.count(old_mobile) != 1:
    raise RuntimeError("expected one stale mobile-label assertion")
path.write_text(text.replace(old_mobile, new_mobile, 1))

Path("tools/fix_pr13_signal_test_hook.py").unlink()
Path(".github/workflows/fix-pr13-signal-test-hook.yml").unlink()
