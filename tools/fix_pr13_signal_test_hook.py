from pathlib import Path

path = Path("tests/test_signal_path_ownership.py")
text = path.read_text()
old = "finalize_signal_path_with_rows"
count = text.count(old)
if count != 2:
    raise RuntimeError(f"expected two stale signal finalizer hooks, found {count}")
path.write_text(text.replace(old, "finalize_signal_path"))
Path("tools/fix_pr13_signal_test_hook.py").unlink()
Path(".github/workflows/fix-pr13-signal-test-hook.yml").unlink()
