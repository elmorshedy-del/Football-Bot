from pathlib import Path


def replace(path, old, new):
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one match, found {count}")
    p.write_text(text.replace(old, new, 1))


replace(
    "tests/test_bid_path.py",
    '''            self.row(2000.0, 70.0),\n            self.row(2500.0, 72.0, availability="terminal", terminal=1),\n''',
    '''            self.row(2000.0, 70.0),\n            self.row(2500.0, None, availability="terminal", terminal=1),\n''',
)
replace(
    "tests/test_bid_path.py",
    '''        self.assertEqual(summary["samples_total"], 5)\n        self.assertEqual(summary["samples_priced"], 4)\n''',
    '''        self.assertEqual(summary["samples_total"], 5)\n        self.assertEqual(summary["samples_priced"], 3)\n''',
)

replace(
    "tests/test_dashboard_browser.py",
    '''        terminal = page.wait_for_selector("#trade-list .bid-path-terminal", timeout=5000)\n        self.assertTrue(terminal.is_visible(), "terminal timestamp has no end marker")\n        path_d = page.get_attribute("#trade-list path.bid-path-line", "d") or ""\n''',
    '''        # A vertical SVG line has a zero-width bounding box, so Playwright's\n        # is_visible() reports false even when its stroke is rendered.  Assert\n        # attachment plus computed stroke instead of using HTML-box visibility.\n        terminal = page.wait_for_selector(\n            "#trade-list .bid-path-terminal", state="attached", timeout=5000,\n        )\n        stroke = page.evaluate("el => getComputedStyle(el).stroke", terminal)\n        self.assertNotIn(stroke, ("", "none", "rgba(0, 0, 0, 0)"),\n                         "terminal timestamp marker has no rendered stroke")\n        path_d = page.get_attribute("#trade-list path.bid-path-line", "d") or ""\n''',
)

Path("tools/apply_pr13_ci_cleanup.py").unlink()
Path(".github/workflows/apply-pr13-ci-cleanup.yml").unlink()
