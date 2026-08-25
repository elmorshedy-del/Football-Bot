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
KALSHI_REST = os.environ.get("KALSHI_REST", "https://api.elections.kalshi.com/trade-api/v2")
KALSHI_WS = os.environ.get("KALSHI_WS", "wss://api.elections.kalshi.com/trade-api/ws/v2")

MODE = os.environ.get("MODE", "auto")  # auto | live | demo
DATA_DIR = os.environ.get("DATA_DIR", "./data")

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

# Per-league Gate A realized edge (cents/contract at 50ms) — dashboard prior; live re-ranks
LEAGUE_PRIOR = {
    "KXLIGAMXGAME": 27, "KXLEAGUESCUPGAME": 27, "KXCLUBFGAME": 18, "KXMLSGAME": 17,
    "KXBRASILEIROGAME": 13, "KXARGPREMDIVGAME": 12, "KXUECLGAME": 14, "KXEPLGAME": 4,
    "KXWCGAME": 0, "KXUCLGAME": 0, "KXUELGAME": 0, "KXCHNSLGAME": 0, "KXDIMAYORGAME": 0,
}


def has_credentials():
    return bool(KALSHI_API_KEY_ID) and bool(KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_PATH)


def mode():
    if MODE in ("live", "demo"):
        return MODE
    return "live" if has_credentials() else "demo"
