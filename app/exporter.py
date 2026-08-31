"""Build a portable, non-secret study archive for external replay/backtesting."""
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile

from . import config, store


EXPORT_SCHEMA = "football.paper_study_export.v1"
TABLES = (
    "markets",
    "signals",
    "trades",
    "paper_fills",
    "latency",
    "goal_latency_observations",
    "match_clock_observations",
    "provider_match_events",
    "eventlog",
)


def non_secret_config():
    """Return an explicit allowlist; never serialize the process environment."""
    names = (
        "DL_MIN", "LEVELS_MIN", "SIZE_MIN", "CONF_MS", "CONF_SIGN",
        "PRICE_CAP", "NOTIONAL_USD", "TARGET", "TIMEOUT_S", "LOCKOUT_S",
        "EPISODE_COOLDOWN_S", "LATE_ONLY", "LATE_WINDOW_MIN", "USE_STOP",
        "STOP_FRAC", "FEE_EXIT_TAKER", "PRICE_ONLY_SLEEVE_MODE",
        "SLEEVE_START_BEFORE_EXPIRY_MIN", "SLEEVE_AFTER_EXPIRY_MIN",
        "STOP_FRAC", "FEE_EXIT_TAKER", "PRICE_ONLY_SLEEVE_MODE",
        "SLEEVE_START_BEFORE_EXPIRY_MIN", "SLEEVE_AFTER_EXPIRY_MIN",
        "SLEEVE_BASELINE_MS", "SLEEVE_MAX_BASELINE_AGE_MS",
        "SLEEVE_TRIPLET_FRESH_MS", "SLEEVE_MAX_SPREAD_C",
        "SLEEVE_MIN_TEAM_GAIN_PP", "SLEEVE_MIN_DRAW_GAIN_PP",
        "SLEEVE_MIN_TEAM_POST", "SLEEVE_MIN_DRAW_POST",
        "SLEEVE_MAX_SIBLING_RISE_PP", "SLEEVE_MIN_EXPLAINED",
        "SLEEVE_SCRATCH_ARM_C", "SLEEVE_SCRATCH_BUFFER_C",
        "SLEEVE_UNKNOWN_FEE_BUFFER_C",
        "SLEEVE_TRAIL_ARM_C", "SLEEVE_TRAIL_MIN_C", "SLEEVE_TRAIL_FRAC",
        "SLEEVE_REVERSAL_C", "SLEEVE_OSCILLATION_WINDOW_S",
        "SLEEVE_OSCILLATION_CROSSES", "SLEEVE_MAX_OSCILLATION_EFFICIENCY",
        "SLEEVE_TIMEOUT_S", "PAPER_EXECUTION_V2", "PAPER_ENTRY_LATENCY_MS",
        "PAPER_EXIT_LATENCY_MS", "PAPER_EXECUTION_POLL_MS",
        "GOAL_LATENCY_OBSERVER", "GOAL_LATENCY_POLL_MS",
        "GOAL_LATENCY_LOOKBACK_S", "GOAL_LATENCY_AFTER_S",
        "EVENT_MATCH_WINDOW_S", "MATCH_CLOCK_MAX_AGE_MS", "SOCCER_SERIES",
    )
    return {name.lower(): getattr(config, name) for name in names}


def _rows(connection, table):
    cursor = connection.execute(f'SELECT * FROM "{table}"')
    columns = [item[0] for item in cursor.description]
    return columns, [dict(zip(columns, row)) for row in cursor.fetchall()]


def _csv_bytes(columns, rows):
    import io
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _jsonl_bytes(rows):
    return "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        for row in rows
    ).encode("utf-8")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(content):
    return hashlib.sha256(content).hexdigest()


def prepare_database_snapshot():
    """Synchronously freeze SQLite at the same boundary as selected raw segments."""
    exports_dir = Path(config.DATA_DIR) / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    fd, snapshot_path = tempfile.mkstemp(
        prefix="study-snapshot-", suffix=".db", dir=exports_dir,
    )
    os.close(fd)
    try:
        store.backup_database(snapshot_path)
    except Exception:
        try:
            os.unlink(snapshot_path)
        except OSError:
            pass
        raise
    return snapshot_path


def raw_feed_paths():
    raw_dir = Path(config.DATA_DIR) / "raw"
    if not raw_dir.exists():
        return []
    return [path for path in sorted(raw_dir.glob("feed-*.jsonl.gz"))
            if path.is_file() and not path.is_symlink()]


def build_study_bundle(output_path=None, mode=None, raw_paths=None, snapshot_path=None):
    """Return ``(zip_path, manifest)`` for a consistent study snapshot."""
    exports_dir = Path(config.DATA_DIR) / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if output_path is None:
        fd, generated = tempfile.mkstemp(
            prefix=f"football-study-{stamp}-", suffix=".zip", dir=exports_dir,
        )
        os.close(fd)
        output_path = generated
    output_path = str(output_path)
    snapshot_path = snapshot_path or prepare_database_snapshot()
    try:
        connection = sqlite3.connect(snapshot_path)
        try:
            schema_rows = connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
            ).fetchall()
            schema_text = ";\n\n".join(row[0].rstrip(";") for row in schema_rows) + ";\n"
            manifest = {
                "schema": EXPORT_SCHEMA,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "mode": mode or "unknown",
                "paper_only": True,
                "guarantee": "none",
                "configuration": non_secret_config(),
                "tables": {},
                "raw_feed": [],
                "artifacts": {},
                "files": {
                    "sqlite_snapshot": "database/footballbot-snapshot.db",
                    "sqlite_schema": "database/schema.sql",
                    "backtest_handoff": "docs/PRICE_ONLY_BACKTEST_HANDOFF.md",
                },
                "audit_semantics": {
                    "match_event_role": "diagnostic_only",
                    "nearest_event_is_causal": False,
                    "event_match_window_s": config.EVENT_MATCH_WINDOW_S,
                    "provider_observation_time_is_not_event_occurrence_time": True,
                },
            }
            with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6) as archive:
                archive.write(snapshot_path, manifest["files"]["sqlite_snapshot"])
                archive.writestr(manifest["files"]["sqlite_schema"], schema_text)
                manifest["artifacts"][manifest["files"]["sqlite_snapshot"]] = {
                    "sha256": _sha256(snapshot_path),
                    "bytes": os.path.getsize(snapshot_path),
                }
                schema_bytes = schema_text.encode("utf-8")
                manifest["artifacts"][manifest["files"]["sqlite_schema"]] = {
                    "sha256": _sha256_bytes(schema_bytes),
                    "bytes": len(schema_bytes),
                }
                for table in TABLES:
                    columns, rows = _rows(connection, table)
                    csv_name = f"tables/{table}.csv"
                    jsonl_name = f"tables/{table}.jsonl"
                    csv_content = _csv_bytes(columns, rows)
                    jsonl_content = _jsonl_bytes(rows)
                    archive.writestr(csv_name, csv_content)
                    archive.writestr(jsonl_name, jsonl_content)
                    manifest["tables"][table] = {
                        "rows": len(rows),
                        "columns": columns,
                        "csv": csv_name,
                        "jsonl": jsonl_name,
                    }
                    for name, content in (
                        (csv_name, csv_content), (jsonl_name, jsonl_content),
                    ):
                        manifest["artifacts"][name] = {
                            "sha256": _sha256_bytes(content),
                            "bytes": len(content),
                        }
                selected_raw = raw_feed_paths() if raw_paths is None else [
                    Path(path) for path in raw_paths
                ]
                for path in selected_raw:
                    if not path.is_file() or path.is_symlink():
                        continue
                    archived = f"raw/{path.name}"
                    # Recorder segments are already gzip-compressed. Re-deflating
                    # them is expensive on long-running volumes and provides no
                    # meaningful size reduction.
                    archive.write(path, archived, compress_type=zipfile.ZIP_STORED)
                    raw_sha256 = _sha256(path)
                    manifest["raw_feed"].append({
                        "file": archived,
                        "bytes": path.stat().st_size,
                        "sha256": raw_sha256,
                    })
                    manifest["artifacts"][archived] = {
                        "sha256": raw_sha256,
                        "bytes": path.stat().st_size,
                    }
                handoff = Path(__file__).resolve().parents[1] / "docs" / \
                    "PRICE_ONLY_BACKTEST_HANDOFF.md"
                archive.write(handoff, manifest["files"]["backtest_handoff"])
                manifest["artifacts"][manifest["files"]["backtest_handoff"]] = {
                    "sha256": _sha256(handoff),
                    "bytes": handoff.stat().st_size,
                }
                readme = (
                    "Paper-study data export. Start with manifest.json and "
                    "docs/PRICE_ONLY_BACKTEST_HANDOFF.md. No return is guaranteed.\n"
                ).encode("utf-8")
                archive.writestr(
                    "README.txt", readme,
                )
                manifest["artifacts"]["README.txt"] = {
                    "sha256": _sha256_bytes(readme),
                    "bytes": len(readme),
                }
                archive.writestr("manifest.json", json.dumps(
                    manifest, indent=2, ensure_ascii=False, sort_keys=True,
                ))
        finally:
            connection.close()
        return output_path, manifest
    except Exception:
        try:
            os.unlink(output_path)
        except OSError:
            pass
        raise
    finally:
        try:
            os.unlink(snapshot_path)
        except OSError:
            pass
