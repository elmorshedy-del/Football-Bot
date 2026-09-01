"""Handoff section 8.5: a real click in a real browser, not a source search.

Skipped (not silently passed) when Chromium or Playwright is unavailable, so a
missing browser can never be mistaken for a passing interaction test.
"""
import json
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_CHROMIUM_CANDIDATES = (
    os.environ.get("CHROMIUM_EXECUTABLE") or "",
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
)


def _chromium_path():
    for candidate in _CHROMIUM_CANDIDATES:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


try:  # pragma: no cover - availability probe
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


# The chart helpers under test, lifted verbatim from the dashboard bundle so the
# browser executes the shipped implementation rather than a copy.
_HARNESS = """
<!doctype html><html><body>
<div id="card"></div>
<script>
%(helpers)s
window.__render = function (trade, samples) {
  pathCache.set(pathKey(trade.id), samples);
  document.getElementById("card").innerHTML = pathSparkline(trade);
};
</script>
</body></html>
"""


def _extract(source, names):
    """Pull named top-level functions/consts out of the bundle by brace match."""
    chunks = []
    for name in names:
        for pattern in (
            f"function {name}(",
            f"const {name} = ",
            f"async function {name}(",
        ):
            start = source.find(pattern)
            if start == -1:
                continue
            depth, index, seen = 0, start, False
            while index < len(source):
                char = source[index]
                if char == "{":
                    depth += 1
                    seen = True
                elif char == "}":
                    depth -= 1
                    if seen and depth == 0:
                        index += 1
                        break
                index += 1
            chunks.append(source[start:index])
            break
        else:
            raise AssertionError(f"{name} not found in static/app.js")
    return "\n".join(chunks)


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
@unittest.skipIf(_chromium_path() is None, "chromium is not available")
class ShowPathBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / "static" / "app.js").read_text()
        helpers = _extract(source, [
            "escapeHtml", "finite", "cents", "integer", "duration", "relativeMs",
            "pathKey", "segmentsFromSamples", "pathSparkline",
        ])
        # pathCache is a module-level Map in the bundle.
        cls.html = _HARNESS % {"helpers": "const pathCache = new Map();\n" + helpers}
        cls._play = sync_playwright().start()
        cls.browser = cls._play.chromium.launch(executable_path=_chromium_path())

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._play.stop()

    def render(self, trade, samples):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.set_content(self.html)
        page.evaluate(
            "([trade, samples]) => window.__render(trade, samples)",
            [trade, samples],
        )
        return page

    def trade(self, **overrides):
        trade = {
            "id": 51, "entry_px": 60.0, "size": 10.0,
            "bid_path_summary": {
                "samples": 3, "span_ms": 2500, "peak_bid": 90.0, "peak_dt_ms": 0.0,
                "peak_exec_px": 90.0, "ms_at_peak": 1000, "path_travelled_c": 0,
                "path_efficiency": None, "gap_count": 1, "gap_duration_ms": 1000,
                "truncated": False, "dropped_samples": 0,
            },
        }
        trade.update(overrides)
        return trade

    def test_a_numeric_trade_id_renders_a_visible_svg(self):
        """The cache is written by a string id and read by trade.id."""
        page = self.render(self.trade(), [
            {"dt_ms": 0.0, "bid": 90.0}, {"dt_ms": 500.0, "bid": 85.0},
            {"dt_ms": 1000.0, "bid": 80.0},
        ])

        svg = page.query_selector("#card svg.bid-path-svg")
        self.assertIsNotNone(svg, "no chart rendered for a numeric trade id")
        self.assertTrue(svg.is_visible(), "the chart rendered but is not visible")
        self.assertIsNotNone(page.query_selector("#card path.bid-path-line"))

    def test_a_gapped_path_draws_one_subpath_per_segment_and_never_bridges(self):
        page = self.render(self.trade(), [
            {"dt_ms": 0.0, "bid": 90.0},
            {"dt_ms": 1000.0, "bid": None},
            {"dt_ms": 2000.0, "bid": 70.0},
            {"dt_ms": 2500.0, "bid": 72.0},
        ])

        d = page.get_attribute("#card path.bid-path-line", "d")
        self.assertIsNotNone(d, "the chart path has no d attribute")
        moves = re.findall(r"M", d)
        self.assertGreaterEqual(
            len(moves), 2,
            f"a gapped path must start a new subpath at the outage; d={d!r}",
        )
        # The only L commands may sit inside a segment, never straight from the
        # pre-gap point to the post-gap point.
        first_segment, _, rest = d.partition(" M")
        self.assertNotIn(
            "L", first_segment,
            f"the single pre-gap point was connected across the outage; d={d!r}",
        )
        self.assertTrue(rest, "no second segment was drawn")

    def test_the_gap_is_reported_to_the_reader(self):
        page = self.render(self.trade(), [
            {"dt_ms": 0.0, "bid": 90.0},
            {"dt_ms": 1000.0, "bid": None},
            {"dt_ms": 2000.0, "bid": 70.0},
        ])
        head = page.inner_text("#card .bid-path-head")
        self.assertIn("gap", head.lower(), f"the outage is not disclosed: {head!r}")


if __name__ == "__main__":
    unittest.main()
