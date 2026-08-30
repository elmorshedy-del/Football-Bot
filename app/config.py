"""Football-Bot configuration. Everything overridable via environment variables."""
import os


def _f(name, default):
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _i(name, default):
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def _b(name, default):
    v = os.environ.get(name, "").lower()
    return default if v == "" else v in ("1", "true", "yes", "on")


# --- Kalshi credentials (absent => DEMO mode) ---
KALSHI_API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY = os.environ.get("KALSHI_PRIVATE_KEY", "")  # PEM string
KALSHI_PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
# Bulletproof escape hatch: base64 of the whole .key file (no newline issues).
# Produce with:  base64 -w0 mykey.key   (or: python -c "import base64;print(base64.b64encode(open('mykey.key','rb').read()).decode())")
KALSHI_PRIVATE_KEY_B64 = os.environ.get("KALSHI_PRIVATE_KEY_B64", "")
KALSHI_REST = os.environ.get("KALSHI_REST", "https://api.elections.kalshi.com/trade-api/v2")
KALSHI_WS = os.environ.get("KALSHI_WS", "wss://api.elections.kalshi.com/trade-api/ws/v2")

MODE = os.environ.get("MODE", "auto")  # auto | live | demo
DATA_DIR = os.environ.get("DATA_DIR", "./data")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

# --- Strategy (frozen Gate A primary unless overridden) ---
DL_MIN = _f("DL_MIN", 0.8)              # min log-odds displacement of the sweep
LEVELS_MIN = _i("LEVELS_MIN", 5)        # min distinct price levels in the sweep
SIZE_MIN = _f("SIZE_MIN", 200.0)        # min contracts in the sweep
CONF_MS = _i("CONF_MS", 50)             # sibling confirmation window (+- ms)
CONF_SIGN = _b("CONF_SIGN", True)       # sibling must move opposite sign (validated improvement)
PRICE_CAP = _f("PRICE_CAP", 58.0)       # max price paid (isotonic zero-crossing, late regime)
NOTIONAL_USD = _f("NOTIONAL_USD", 100.0)
TARGET = _f("TARGET", 90.0)             # take-profit (YES-space for longs, mirrored for NO)
TIMEOUT_S = _i("TIMEOUT_S", 180)        # max hold before flattening
LOCKOUT_S = _i("LOCKOUT_S", 120)        # per-market re-entry lockout
EPISODE_COOLDOWN_S = _i("EPISODE_COOLDOWN_S", 5)
LATE_ONLY = _b("LATE_ONLY", False)      # if true, only trade within LATE_WINDOW_MIN of scheduled close
LATE_WINDOW_MIN = _i("LATE_WINDOW_MIN", 20)
USE_STOP = _b("USE_STOP", False)        # Gate A: stops off; shadow-stop is always recorded
STOP_FRAC = _f("STOP_FRAC", 0.35)
FEE_EXIT_TAKER = _b("FEE_EXIT_TAKER", True)  # charge taker fee on exits (conservative)

# --- Price-only late-score sleeve (paper-only; no score/event feed) ---
# ``off`` preserves Gate A exactly. ``enforce`` runs only the price-only sleeve.
# ``parallel`` independently paper-trades both Gate A and the price-only sleeve.
_price_only_sleeve_mode = os.environ.get("PRICE_ONLY_SLEEVE_MODE", "off").lower()
PRICE_ONLY_SLEEVE_MODE = (
    _price_only_sleeve_mode
    if _price_only_sleeve_mode in {"off", "enforce", "parallel"}
    else "off"
)
SLEEVE_START_BEFORE_EXPIRY_MIN = _f("SLEEVE_START_BEFORE_EXPIRY_MIN", 2.0)
SLEEVE_AFTER_EXPIRY_MIN = _f("SLEEVE_AFTER_EXPIRY_MIN", 12.0)
SLEEVE_BASELINE_MS = _f("SLEEVE_BASELINE_MS", 1800.0)
SLEEVE_MAX_BASELINE_AGE_MS = _f("SLEEVE_MAX_BASELINE_AGE_MS", 6000.0)
SLEEVE_TRIPLET_FRESH_MS = _f("SLEEVE_TRIPLET_FRESH_MS", 5000.0)
SLEEVE_MAX_SPREAD_C = _f("SLEEVE_MAX_SPREAD_C", 8.0)
SLEEVE_MIN_TEAM_GAIN_PP = _f("SLEEVE_MIN_TEAM_GAIN_PP", 0.15)
SLEEVE_MIN_DRAW_GAIN_PP = _f("SLEEVE_MIN_DRAW_GAIN_PP", 0.15)
SLEEVE_MIN_TEAM_POST = _f("SLEEVE_MIN_TEAM_POST", 0.45)
SLEEVE_MIN_DRAW_POST = _f("SLEEVE_MIN_DRAW_POST", 0.40)
SLEEVE_MAX_SIBLING_RISE_PP = _f("SLEEVE_MAX_SIBLING_RISE_PP", 0.02)
SLEEVE_MIN_EXPLAINED = _f("SLEEVE_MIN_EXPLAINED", 0.85)
SLEEVE_SCRATCH_ARM_C = _f("SLEEVE_SCRATCH_ARM_C", 4.0)
SLEEVE_SCRATCH_BUFFER_C = _f("SLEEVE_SCRATCH_BUFFER_C", 0.5)
SLEEVE_UNKNOWN_FEE_BUFFER_C = _f("SLEEVE_UNKNOWN_FEE_BUFFER_C", 2.0)
SLEEVE_TRAIL_ARM_C = _f("SLEEVE_TRAIL_ARM_C", 6.0)
SLEEVE_TRAIL_MIN_C = _f("SLEEVE_TRAIL_MIN_C", 2.0)
SLEEVE_TRAIL_FRAC = _f("SLEEVE_TRAIL_FRAC", 0.45)
SLEEVE_REVERSAL_C = _f("SLEEVE_REVERSAL_C", 2.0)
SLEEVE_OSCILLATION_WINDOW_S = _f("SLEEVE_OSCILLATION_WINDOW_S", 4.0)
SLEEVE_OSCILLATION_CROSSES = _i("SLEEVE_OSCILLATION_CROSSES", 3)
SLEEVE_MAX_OSCILLATION_EFFICIENCY = _f("SLEEVE_MAX_OSCILLATION_EFFICIENCY", 0.35)
SLEEVE_TIMEOUT_S = _f("SLEEVE_TIMEOUT_S", 30.0)

# --- Paper execution adapter (off preserves the original paper desk exactly) ---
PAPER_EXECUTION_V2 = _b("PAPER_EXECUTION_V2", False)
PAPER_ENTRY_LATENCY_MS = _f("PAPER_ENTRY_LATENCY_MS", 150.0)
PAPER_EXIT_LATENCY_MS = _f("PAPER_EXIT_LATENCY_MS", 150.0)
PAPER_EXECUTION_POLL_MS = _f("PAPER_EXECUTION_POLL_MS", 5.0)

# --- Read-only Kalshi goal/market latency observer ---
# This never participates in signal generation or paper execution.  It polls
# Kalshi's milestone live-data endpoint and timestamps score changes beside the
# already-received market stream so feed latency can be measured empirically.
GOAL_LATENCY_OBSERVER = _b("GOAL_LATENCY_OBSERVER", True)
GOAL_LATENCY_POLL_MS = _f("GOAL_LATENCY_POLL_MS", 250.0)
GOAL_LATENCY_LOOKBACK_S = _f("GOAL_LATENCY_LOOKBACK_S", 10.0)
GOAL_LATENCY_AFTER_S = _f("GOAL_LATENCY_AFTER_S", 2.0)
EVENT_MATCH_WINDOW_S = _f("EVENT_MATCH_WINDOW_S", 20.0)

# --- Market discovery ---
DISCOVERY_INTERVAL_S = _i("DISCOVERY_INTERVAL_S", 180)
SUBSCRIBE_BEFORE_CLOSE_MIN = _i("SUBSCRIBE_BEFORE_CLOSE_MIN", 150)  # watch markets closing within this
DROP_AFTER_CLOSE_MIN = _i("DROP_AFTER_CLOSE_MIN", 20)
SERIES_FILE = os.environ.get("SERIES_FILE", "")  # optional path to override the soccer series list

# --- Demo replay ---
DEMO_SPEED = _f("DEMO_SPEED", 25.0)     # x real time
DEMO_LOOP = _b("DEMO_LOOP", True)

# --- Dashboard ---
BROADCAST_COALESCE_MS = _i("BROADCAST_COALESCE_MS", 250)

# Soccer game series (from the Aug 2026 universe scan; league -> [series tickers])
SOCCER_SERIES = [
    "KXEPLGAME", "KXLALIGAGAME", "KXBUNDESLIGAGAME", "KXSERIEAGAME", "KXLIGUE1GAME",
    "KXMLSGAME", "KXLIGAMXGAME", "KXLEAGUESCUPGAME", "KXBRASILEIROGAME", "KXBRASILEIROBGAME",
    "KXARGPREMDIVGAME", "KXNWSLGAME", "KXUCLGAME", "KXUELGAME", "KXUECLGAME", "KXCLUBFGAME",
    "KXALLSVENSKANGAME", "KXELITESERIENGAME", "KXDENSUPERLIGAGAME", "KXEREDIVISIEGAME",
    "KXLIGAPORTUGALGAME", "KXSCOTTISHPREMGAME", "KXEFLCHAMPIONSHIPGAME", "KXEFLCUPGAME",
    "KXSAUDIPLGAME", "KXCHNSLGAME", "KXJLEAGUEGAME", "KXKLEAGUEGAME", "KXECULPGAME",
    "KXPERLIGA1GAME", "KXCHLLDPGAME", "KXDIMAYORGAME", "KXURYPDGAME", "KXCOPADELREYGAME",
    "KXFACUPGAME", "KXDFBPOKALGAME", "KXCOPPAITALIAGAME", "KXCOUPEDEFRANCEGAME",
    "KXCONMEBOLLIBGAME", "KXCONMEBOLSUDGAME", "KXCONCACAFCCUPGAME", "KXBELGIANPLGAME",
    "KXLIGAEXPGAME", "KXBRASILEIROCGAME", "KXCZEFLGAME", "KXEKSTRAKLASAGAME",
]

LEAGUE_NAMES = {
    "KXEPLGAME": "English Premier League",
    "KXLALIGAGAME": "La Liga",
    "KXBUNDESLIGAGAME": "Bundesliga",
    "KXSERIEAGAME": "Serie A",
    "KXLIGUE1GAME": "Ligue 1",
    "KXMLSGAME": "Major League Soccer",
    "KXLIGAMXGAME": "Liga MX",
    "KXLEAGUESCUPGAME": "Leagues Cup",
    "KXBRASILEIROGAME": "Brazilian Serie A",
    "KXBRASILEIROBGAME": "Brazilian Serie B",
    "KXARGPREMDIVGAME": "Argentine Primera Division",
    "KXNWSLGAME": "National Women's Soccer League",
    "KXUCLGAME": "UEFA Champions League",
    "KXUELGAME": "UEFA Europa League",
    "KXUECLGAME": "UEFA Conference League",
    "KXCLUBFGAME": "FIFA Club World Cup",
    "KXALLSVENSKANGAME": "Allsvenskan",
    "KXELITESERIENGAME": "Eliteserien",
    "KXDENSUPERLIGAGAME": "Danish Superliga",
    "KXEREDIVISIEGAME": "Eredivisie",
    "KXLIGAPORTUGALGAME": "Primeira Liga",
    "KXSCOTTISHPREMGAME": "Scottish Premiership",
    "KXEFLCHAMPIONSHIPGAME": "EFL Championship",
    "KXEFLCUPGAME": "EFL Cup",
    "KXSAUDIPLGAME": "Saudi Pro League",
    "KXCHNSLGAME": "Chinese Super League",
    "KXJLEAGUEGAME": "J1 League",
    "KXKLEAGUEGAME": "K League 1",
    "KXECULPGAME": "Ecuadorian LigaPro",
    "KXPERLIGA1GAME": "Peruvian Liga 1",
    "KXCHLLDPGAME": "Chilean Primera Division",
    "KXDIMAYORGAME": "Colombian Primera A",
    "KXURYPDGAME": "Uruguayan Primera Division",
    "KXCOPADELREYGAME": "Copa del Rey",
    "KXFACUPGAME": "FA Cup",
    "KXDFBPOKALGAME": "DFB-Pokal",
    "KXCOPPAITALIAGAME": "Coppa Italia",
    "KXCOUPEDEFRANCEGAME": "Coupe de France",
    "KXCONMEBOLLIBGAME": "Copa Libertadores",
    "KXCONMEBOLSUDGAME": "Copa Sudamericana",
    "KXCONCACAFCCUPGAME": "CONCACAF Champions Cup",
    "KXBELGIANPLGAME": "Belgian Pro League",
    "KXLIGAEXPGAME": "Liga de Expansion MX",
    "KXBRASILEIROCGAME": "Brazilian Serie C",
    "KXCZEFLGAME": "Czech First League",
    "KXEKSTRAKLASAGAME": "Ekstraklasa",
}

# Per-league Gate A realized edge (cents/contract at 50ms) — dashboard prior; live re-ranks
LEAGUE_PRIOR = {
    "KXLIGAMXGAME": 27, "KXLEAGUESCUPGAME": 27, "KXCLUBFGAME": 18, "KXMLSGAME": 17,
    "KXBRASILEIROGAME": 13, "KXARGPREMDIVGAME": 12, "KXUECLGAME": 14, "KXEPLGAME": 4,
    "KXWCGAME": 0, "KXUCLGAME": 0, "KXUELGAME": 0, "KXCHNSLGAME": 0, "KXDIMAYORGAME": 0,
}


def has_credentials():
    return bool(KALSHI_API_KEY_ID) and bool(
        KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY_B64)


def mode():
    if MODE in ("live", "demo"):
        return MODE
    return "live" if has_credentials() else "demo"
