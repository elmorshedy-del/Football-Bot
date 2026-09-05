"""Cloudflare R2 archive for the raw WebSocket feed.

The Railway volume is finite.  On 2026-09-05 it held 4.00 GB of a 4.08 GB
maximum, 2.94 GB of that being 176 hourly raw segments (`feed-YYYYMMDD-HH`,
Aug 25 18:00 -> Sep 5) and the rest the study database.  An audit export
already failed with `study_export: database or disk is full`, and at ~300 MB of
new segments a day the next failure is silent write loss in SQLite.

This module extends that volume onto object storage as ONE logical archive with
ONE timeline, never two stores that can disagree:

* every segment is in exactly one state -- `local`, `uploaded`, `pruned` --
  recorded in `raw_segments`, never inferred from a directory listing;
* only a SEALED segment is uploaded (the hour being appended to is untouchable);
* a local copy is deleted only after its remote copy has been verified twice
  (returned ETag == local md5, then HEAD content-length == local size), and the
  single delete site re-checks every one of those facts against SQLite and
  against R2 immediately before unlinking;
* reads are transparent: `exporter.raw_inventory()` and
  `GET /api/export/raw/{name}` serve local and remote segments alike;
* `archive_continuity()` states what the whole timeline contains, including the
  hours that are missing because the bot was down.

R2's S3-compatible API needs AWS Signature Version 4.  The dependency set is
pinned and minimal, so `SigV4Signer` implements it with stdlib `hmac`/`hashlib`
over the existing `httpx` rather than adding boto3.  It is validated against
the published AWS test vectors in `tests/test_raw_archive.py`.

Nothing here touches trading.  The knobs are storage knobs, deliberately absent
from `config.STRATEGY_PARAM_NAMES`: capturing or moving a recorded file cannot
change a Gate A detection, a confirmation, a size, an entry or an exit.
"""
import asyncio
import calendar
import hashlib
import hmac
import os
import re
import shutil
import threading
import time
from urllib.parse import quote, urlsplit

import httpx

from . import config, store

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
ALGORITHM = "AWS4-HMAC-SHA256"
HOUR_FORMAT = "%Y%m%d-%H"
SEGMENT_NAME = re.compile(r"^feed-(\d{8}-\d{2})(?:-part-\d+)?\.jsonl\.gz$")
_CHUNK = 1024 * 1024
# One upload at a time with a pause between: the backlog must drain without
# saturating either the process or the uplink the live WebSocket shares.
_UPLOADS_PER_TICK = 12
_UPLOAD_SLEEP_S = 0.5
# `Engine.status()` runs on the event loop, so the continuity report it returns
# is a cached snapshot.  The background tick refreshes it off the loop; this TTL
# only bounds staleness when the archive is disabled and nothing else refreshes.
_CONTINUITY_TTL_S = 30.0
# A stray old segment must not turn the missing-hour list into a million rows.
_MAX_MISSING_HOURS = 720

ARCHIVE_UPLOADED = "archive_uploaded"
ARCHIVE_VERIFIED = "archive_verified"
ARCHIVE_PRUNED = "archive_pruned"
ARCHIVE_ERROR = "archive_error"


class ArchiveError(Exception):
    """A recorded, retryable archive failure. Never fatal to the recorder."""


def raw_dir():
    return os.path.join(config.DATA_DIR, "raw")


def current_hour(now=None):
    return time.strftime(HOUR_FORMAT, time.gmtime(now))


def segment_hour(name):
    """The UTC hour a segment belongs to, or None when the name is not ours."""
    match = SEGMENT_NAME.match(name or "")
    return match.group(1) if match else None


def hour_epoch(hour):
    """Start of an hour label as a UTC epoch, or None when unparseable."""
    try:
        return float(calendar.timegm(time.strptime(hour, HOUR_FORMAT)))
    except (TypeError, ValueError):
        return None


def local_segments():
    """Segment files currently on the volume, oldest name first."""
    directory = raw_dir()
    found = {}
    try:
        entries = os.scandir(directory)
    except OSError:
        return found
    with entries:
        for entry in entries:
            if not SEGMENT_NAME.match(entry.name):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                found[entry.name] = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return dict(sorted(found.items()))


def file_digests(path, chunk=_CHUNK):
    """Stream one pass over the file returning (sha256, md5, bytes).

    Both digests come from the same read: sha256 is what SigV4 signs and what
    the study manifest records, md5 is what R2 returns as the ETag for a
    single-part PUT and is therefore what proves the upload arrived intact.
    """
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            sha.update(block)
            md5.update(block)
            size += len(block)
    return sha.hexdigest(), md5.hexdigest(), size


# --- AWS Signature Version 4 -------------------------------------------------

class SigV4Signer:
    """Minimal SigV4 request signer (stdlib only).

    Validated in `tests/test_raw_archive.py` against the published AWS test
    vectors -- `get-vanilla`, `post-vanilla-query`, and the S3 "GET Object with
    Range" and "PUT Object" examples -- so it is not shipped unverified.
    """

    def __init__(self, access_key, secret_key, region="auto", service="s3"):
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.service = service

    @staticmethod
    def amz_date(now=None):
        return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))

    @staticmethod
    def canonical_uri(path):
        # Encoded exactly once: S3-compatible services do not normalise or
        # double-encode the object path.
        return quote(path or "/", safe="/-_.~")

    @staticmethod
    def canonical_query(query):
        if not query:
            return ""
        pairs = []
        for part in query.split("&"):
            if not part:
                continue
            key, _, value = part.partition("=")
            pairs.append((key, value))
        return "&".join(
            f"{key}={value}" for key, value in sorted(pairs)
        )

    def canonical_request(self, method, path, query, headers, payload_sha256):
        names = sorted(headers)
        canonical_headers = "".join(
            f"{name}:{' '.join(str(headers[name]).split())}\n" for name in names
        )
        signed_headers = ";".join(names)
        request = "\n".join([
            method.upper(),
            self.canonical_uri(path),
            self.canonical_query(query),
            canonical_headers,
            signed_headers,
            payload_sha256,
        ])
        return request, signed_headers

    def _signing_key(self, date):
        key = ("AWS4" + self.secret_key).encode("utf-8")
        for part in (date, self.region, self.service, "aws4_request"):
            key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
        return key

    def authorization(self, method, url, headers, payload_sha256, amz_date):
        """Return the Authorization header value for an already-built request.

        `headers` must be the exact lowercase header map that will be sent and
        signed; anything not in it is an unsigned header, which SigV4 allows.
        """
        split = urlsplit(url)
        canonical, signed_headers = self.canonical_request(
            method, split.path, split.query, headers, payload_sha256,
        )
        date = amz_date[:8]
        scope = f"{date}/{self.region}/{self.service}/aws4_request"
        string_to_sign = "\n".join([
            ALGORITHM,
            amz_date,
            scope,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        ])
        signature = hmac.new(
            self._signing_key(date), string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return (
            f"{ALGORITHM} Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

    def sign(self, method, url, payload_sha256=EMPTY_SHA256, extra=None,
             amz_date=None):
        """Build the full signed header set for one request."""
        split = urlsplit(url)
        stamp = amz_date or self.amz_date()
        headers = {
            "host": split.netloc,
            "x-amz-content-sha256": payload_sha256,
            "x-amz-date": stamp,
        }
        for key, value in (extra or {}).items():
            if value is not None:
                headers[key.lower()] = value
        headers["Authorization"] = self.authorization(
            method, url, dict(headers), payload_sha256, stamp,
        )
        return headers


# --- R2 client ---------------------------------------------------------------

class R2Client:
    """Path-style S3 client over `httpx`: PUT, HEAD and a streaming GET."""

    def __init__(self, endpoint, bucket, access_key, secret_key, region="auto",
                 timeout=120.0):
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.timeout = timeout
        self.signer = SigV4Signer(access_key, secret_key, region=region)

    @classmethod
    def from_config(cls):
        """Build a client, or None when the archive is not fully configured."""
        if not config.raw_archive_ready():
            return None
        return cls(
            config.r2_endpoint(), config.R2_BUCKET,
            config.R2_ACCESS_KEY_ID, config.R2_SECRET_ACCESS_KEY,
        )

    def url_for(self, key):
        return f"{self.endpoint}/{self.bucket}/{quote(key, safe='/-_.~')}"

    def put_file(self, key, path, payload_sha256):
        """Single PUT of a whole segment (R2 accepts up to 5 GB per PUT).

        The body is streamed from the file handle -- httpx reads it in 64 KB
        blocks and derives Content-Length from the file size -- so a 118 MB
        segment is never held in memory.
        """
        url = self.url_for(key)
        headers = self.signer.sign(
            "PUT", url, payload_sha256,
            extra={"content-type": "application/gzip"},
        )
        with open(path, "rb") as body:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.put(url, headers=headers, content=body)
        return response

    def head(self, key):
        url = self.url_for(key)
        headers = self.signer.sign("HEAD", url)
        with httpx.Client(timeout=self.timeout) as client:
            return client.request("HEAD", url, headers=headers)

    async def open_stream(self, key, range_header=None):
        """Open a streaming GET; returns (response, client) both left open.

        The caller owns closing them -- `main.download_raw_segment` hands the
        body to a `StreamingResponse` and closes both when it is exhausted.
        """
        url = self.url_for(key)
        extra = {"range": range_header} if range_header else None
        headers = self.signer.sign("GET", url, extra=extra)
        client = httpx.AsyncClient(timeout=self.timeout)
        try:
            request = client.build_request("GET", url, headers=headers)
            response = await client.send(request, stream=True)
        except Exception:
            await client.aclose()
            raise
        return response, client


# --- Continuity report -------------------------------------------------------

def archive_continuity(mode=None, rows=None, present=None):
    """Describe the whole archive as one timeline.

    Deliberately built from the UNION of the ledger and the volume: a segment
    sealed seconds ago that the background task has not registered yet is still
    part of the timeline, and a row whose file has gone is still part of it too.
    `missing_hours` lists hours between the first and the last with no segment
    at all -- legitimate when the bot was down, but it must be visible rather
    than silently interpolated.
    """
    if rows is None:
        try:
            selector = "all" if mode is None else mode
            rows = store.raw_segment_rows(selector=selector)
        except Exception:  # noqa: BLE001 - a report must never break a caller
            rows = []
    if present is None:
        present = local_segments()
    by_name = {row["name"]: row for row in rows}
    names = sorted(set(by_name) | set(present))

    counts = {state: 0 for state in store.RAW_STATES}
    counts["unregistered"] = 0
    local_bytes = 0
    remote_bytes = 0
    hours = set()
    for name in names:
        row = by_name.get(name)
        state = (row or {}).get("state")
        if state in counts:
            counts[state] += 1
        else:
            counts["unregistered"] += 1
        if name in present:
            local_bytes += present[name]
        if state in (store.RAW_UPLOADED, store.RAW_PRUNED):
            remote_bytes += (row or {}).get("bytes") or 0
        hour = (row or {}).get("hour") or segment_hour(name)
        if hour:
            hours.add(hour)

    ordered = sorted(hours)
    missing = []
    truncated = False
    if len(ordered) > 1:
        start, end = hour_epoch(ordered[0]), hour_epoch(ordered[-1])
        if start is not None and end is not None:
            cursor = start + 3600.0
            while cursor < end:
                label = current_hour(cursor)
                if label not in hours:
                    if len(missing) >= _MAX_MISSING_HOURS:
                        truncated = True
                        break
                    missing.append(label)
                cursor += 3600.0
    return {
        # Three separate facts, never conflated: the switch, whether R2 is
        # credentialled, and whether the archive is therefore actually running.
        # "enabled but not active" is exactly the state a half-configured
        # deployment is in, and it must be visible as that.
        "enabled": bool(config.RAW_ARCHIVE_ENABLED),
        "credentialled": bool(
            config.R2_ACCESS_KEY_ID and config.R2_SECRET_ACCESS_KEY
            and config.R2_BUCKET and config.r2_endpoint()),
        "active": bool(config.raw_archive_ready()),
        "bucket": config.R2_BUCKET,
        "segments": len(names),
        "by_state": counts,
        "local_bytes": local_bytes,
        "remote_bytes": remote_bytes,
        "first_hour": ordered[0] if ordered else None,
        "last_hour": ordered[-1] if ordered else None,
        "hours_present": len(ordered),
        "missing_hours": missing,
        "missing_hours_truncated": truncated,
        "retention_hours": config.RAW_LOCAL_RETENTION_HOURS,
        "min_free_mb": config.RAW_ARCHIVE_MIN_FREE_MB,
    }


# --- The archive itself ------------------------------------------------------

class RawArchive:
    """State machine, backlog drain, retention and continuity for raw segments.

    Everything here runs OFF the event loop: `run()` awaits `tick()` inside
    `asyncio.to_thread`, and the only work done on the loop is a set insertion
    when the recorder seals a segment.  A failure records `last_error`, a feed
    event and an engine error, and never propagates into the recorder, the
    WebSocket path or the paper desk.
    """

    def __init__(self, on_event=None, on_error=None, client=None,
                 active_hour=None):
        self.on_event = on_event
        self.on_error = on_error
        self._client = client
        self._active_hour = active_hour
        self._lock = threading.Lock()
        self._sealed = set()
        self._backoff = {}
        self._stop = threading.Event()
        self._continuity = None
        self._continuity_ts = 0.0
        self.uploaded = 0
        self.verified = 0
        self.pruned = 0
        self.failures = 0
        self.event_failures = 0
        self.last_error = None
        self.last_tick_ts = None
        self.backfill_started = False

    # -- configuration ------------------------------------------------------
    @property
    def enabled(self):
        """Fail closed: no credentials or the switch off means fully inert."""
        return bool(self._client) or config.raw_archive_ready()

    def client(self):
        if self._client is not None:
            return self._client
        return R2Client.from_config()

    def bind_recorder(self, recorder):
        """Learn which hour the recorder still holds open.

        The wall-clock hour is not enough: after an hour boundary with no
        traffic the recorder's handle is still open on the PREVIOUS hour's
        file, and uploading that would archive a truncated gzip member.
        """
        self._active_hour = lambda: getattr(recorder, "_hour", None)

    def active_names(self):
        """Segment names that must never be uploaded, verified or pruned."""
        names = {f"feed-{current_hour()}.jsonl.gz"}
        try:
            open_hour = self._active_hour() if self._active_hour else None
        except Exception:  # noqa: BLE001 - a probe must not break the archive
            open_hour = None
        if open_hour:
            names.add(f"feed-{open_hour}.jsonl.gz")
        return names

    # -- plumbing -----------------------------------------------------------
    def _emit(self, kind, detail):
        """Feed-health ledger entry; `marker=False` -- never re-enter the gzip
        handle from the archive thread."""
        try:
            if self.on_event is not None:
                self.on_event(kind, detail, False)
            else:
                store.insert_feed_event(kind, detail)
        except Exception:  # noqa: BLE001 - the ledger must not break the archive
            self.event_failures += 1

    def _fail(self, name, error):
        """Record a retryable failure everywhere it must be visible."""
        message = f"{type(error).__name__}: {error}" if isinstance(
            error, BaseException) else str(error)
        self.failures += 1
        self.last_error = message
        if name:
            try:
                store.record_raw_segment_error(name, message)
            except Exception:  # noqa: BLE001
                pass
        self._emit(ARCHIVE_ERROR, {"segment": name, "error": message})
        if self.on_error is not None:
            try:
                self.on_error("raw_archive", message)
            except Exception:  # noqa: BLE001
                pass

    def on_sealed(self, path):
        """Recorder hook: a segment is finished being written to.

        Called on the WebSocket path (hour rotation) and on the export worker
        thread (`checkpoint_for_export`), so it does the smallest possible
        amount of work -- one set insertion -- and never touches SQLite here.
        The background tick registers it; the tick's reconcile scan also finds
        it, so a dropped hook costs promptness, never correctness.
        """
        if not self.enabled or not path:
            return
        name = os.path.basename(path)
        if not SEGMENT_NAME.match(name):
            return
        with self._lock:
            self._sealed.add(name)

    def stop(self):
        self._stop.set()

    # -- state machine ------------------------------------------------------
    def register_pending(self):
        """Move hooked seals and any unregistered file on disk into the ledger.

        This is the startup reconcile as well as the steady-state one: any
        `feed-*.jsonl.gz` that is not the active hour and not in the table is
        registered as `local`, so the ledger describes the volume exactly.
        """
        with self._lock:
            sealed = sorted(self._sealed)
            self._sealed.clear()
        present = local_segments()
        active = self.active_names()
        registered = 0
        known = set()
        try:
            known = {row["name"] for row in store.raw_segment_rows()}
        except Exception as exc:  # noqa: BLE001
            self._fail(None, exc)
            return 0
        for name in sorted(set(sealed) | set(present)):
            if name in known or name in active:
                continue
            try:
                if store.register_raw_segment(
                    name, segment_hour(name), present.get(name), time.time(),
                ):
                    registered += 1
            except Exception as exc:  # noqa: BLE001
                self._fail(name, exc)
        return registered

    def pending_uploads(self):
        """Verified-nothing-yet segments, oldest first, minus the active hour.

        Oldest first is what drains the existing backlog in timeline order and
        frees the oldest disk first.
        """
        active = self.active_names()
        present = local_segments()
        now = time.monotonic()
        pending = []
        for row in store.raw_segment_rows(state=store.RAW_LOCAL):
            name = row["name"]
            if name in active or name not in present:
                continue
            if (row.get("attempts") or 0) >= config.RAW_ARCHIVE_MAX_ATTEMPTS:
                continue
            if self._backoff.get(name, 0.0) > now:
                continue
            pending.append(row)
        return pending

    def upload(self, row):
        """Upload one sealed segment and verify it twice. True when verified.

        Verification is deliberately two independent checks: the ETag returned
        by the PUT must equal the md5 computed while reading the file, and a
        subsequent HEAD must report the same content length as the local file.
        Either one failing leaves the row `local` -- so the segment stays on the
        volume, is retried with backoff, and can never be pruned.
        """
        name = row["name"]
        path = os.path.join(raw_dir(), name)
        client = self.client()
        if client is None:
            return False
        try:
            sha256, md5, size = file_digests(path)
        except OSError as exc:
            self._fail(name, exc)
            self._defer(name, row)
            return False
        key = f"raw/{name}"
        try:
            response = client.put_file(key, path, sha256)
            if response.status_code not in (200, 201):
                raise ArchiveError(
                    f"PUT {key} returned {response.status_code}")
            etag = (response.headers.get("ETag") or "").strip('"')
            if etag.lower() != md5:
                raise ArchiveError(
                    f"ETag {etag or 'missing'} does not match local md5 {md5}")
            head = client.head(key)
            if head.status_code != 200:
                raise ArchiveError(f"HEAD {key} returned {head.status_code}")
            remote_size = int(head.headers.get("Content-Length") or -1)
            if remote_size != size:
                raise ArchiveError(
                    f"remote length {remote_size} does not match local {size}")
        except Exception as exc:  # noqa: BLE001 - every failure is retryable
            self._fail(name, exc)
            self._defer(name, row)
            return False
        now = time.time()
        store.mark_raw_segment_uploaded(
            name, size=size, sha256=sha256, md5=md5, r2_key=key, etag=etag,
            uploaded_ts=now, verified_ts=now,
        )
        self.uploaded += 1
        self.verified += 1
        self._backoff.pop(name, None)
        detail = {"segment": name, "key": key, "bytes": size, "etag": etag}
        self._emit(ARCHIVE_UPLOADED, detail)
        self._emit(ARCHIVE_VERIFIED, {**detail, "sha256": sha256})
        return True

    def _defer(self, name, row):
        """Exponential in-memory backoff; `attempts` in SQLite is the real cap."""
        attempts = (row.get("attempts") or 0) + 1
        delay = min(2 ** attempts, 32) * max(1.0, config.RAW_ARCHIVE_INTERVAL_S)
        self._backoff[name] = time.monotonic() + delay

    def free_bytes(self):
        try:
            return shutil.disk_usage(config.DATA_DIR).free
        except OSError:
            return None

    def prune_candidates(self, now=None):
        """Verified segments still on the volume, oldest first.

        `pruned` rows whose file survived a failed unlink are included so the
        leftover is swept on the next pass; the active hour never is.
        """
        now = time.time() if now is None else now
        active = self.active_names()
        present = local_segments()
        candidates = []
        for row in store.raw_segment_rows():
            name = row["name"]
            if name in active or name not in present:
                continue
            if row.get("state") not in (store.RAW_UPLOADED, store.RAW_PRUNED):
                continue
            if not row.get("verified_ts") or not row.get("r2_key"):
                continue
            candidates.append(row)
        return candidates

    def _expired(self, row, now):
        """True when the segment's hour ended more than the retention ago."""
        start = hour_epoch(row.get("hour") or segment_hour(row["name"]))
        if start is None:
            return False
        sealed = start + 3600.0
        return (now - sealed) >= config.RAW_LOCAL_RETENTION_HOURS * 3600.0

    def prune(self, now=None):
        """Retention plus the low-disk floor, oldest first. Returns the count.

        Pruning is bounded and deliberate: a verified segment is removed only
        because it is older than the retention window, or because free space on
        the volume fell below the floor -- never merely because it was uploaded.
        """
        now = time.time() if now is None else now
        candidates = self.prune_candidates(now)
        removed = 0
        for row in candidates:
            if self._stop.is_set():
                break
            if not self._expired(row, now):
                continue
            if self._prune_one(row):
                removed += 1
        floor = config.RAW_ARCHIVE_MIN_FREE_MB * 1024 * 1024
        if floor > 0:
            for row in self.prune_candidates(now):
                if self._stop.is_set():
                    break
                free = self.free_bytes()
                if free is None or free >= floor:
                    break
                if self._prune_one(row, reason="low_disk"):
                    removed += 1
        return removed

    def _prune_one(self, row, reason="retention"):
        """THE ONLY PLACE A RECORDED SEGMENT IS DELETED FROM THE VOLUME.

        Every guard is re-checked here, against SQLite and against R2, at the
        moment of deletion rather than when the candidate list was built:

        1. the row still exists and is `uploaded` (or a `pruned` leftover);
        2. it carries `verified_ts` and an `r2_key` -- it was verified, not
           merely uploaded;
        3. it is not the hour the recorder is appending to;
        4. the file on disk is still exactly the size that was verified;
        5. a live HEAD confirms the remote object is still there, still that
           size;
        6. the ledger flip `uploaded -> pruned` succeeded, itself conditional
           on `verified_ts IS NOT NULL`.

        Any of these failing means the file stays. The cost of refusing is a
        fuller volume for another minute; the cost of deleting wrongly is
        unrecoverable research data.
        """
        name = row["name"]
        path = os.path.join(raw_dir(), name)
        try:
            current = store.raw_segment(name)
        except Exception as exc:  # noqa: BLE001
            self._fail(name, exc)
            return False
        if current is None:
            return False
        if current.get("state") not in (store.RAW_UPLOADED, store.RAW_PRUNED):
            return False
        if not current.get("verified_ts") or not current.get("r2_key"):
            return False
        if name in self.active_names():
            return False
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        if current.get("bytes") is not None and size != current["bytes"]:
            self._fail(name, ArchiveError(
                f"local size {size} no longer matches verified {current['bytes']}"))
            return False
        client = self.client()
        if client is None:
            return False
        try:
            head = client.head(current["r2_key"])
            if head.status_code != 200:
                raise ArchiveError(
                    f"HEAD {current['r2_key']} returned {head.status_code}")
            remote = int(head.headers.get("Content-Length") or -1)
            if remote != size:
                raise ArchiveError(
                    f"remote length {remote} does not match local {size}")
        except Exception as exc:  # noqa: BLE001 - unverified means undeleted
            self._fail(name, exc)
            return False
        now = time.time()
        if current["state"] == store.RAW_UPLOADED and not \
                store.mark_raw_segment_pruned(name, now):
            return False
        try:
            os.unlink(path)
        except OSError as exc:
            self._fail(name, exc)
            return False
        self.pruned += 1
        self._emit(ARCHIVE_PRUNED, {
            "segment": name, "bytes": size, "reason": reason,
            "key": current["r2_key"],
        })
        return True

    # -- the background task ------------------------------------------------
    def tick(self):
        """One archive pass, off the event loop. Never raises.

        Order matters: register first so the ledger describes the volume,
        upload next so nothing is pruned that was not verified in this same
        pass or an earlier one, prune last.
        """
        registered = uploaded = removed = 0
        if not self.enabled:
            # Inert: no registration, no upload, no deletion, no error.
            self.last_tick_ts = time.time()
            return {"registered": 0, "uploaded": 0, "pruned": 0}
        try:
            registered = self.register_pending()
            for row in self.pending_uploads()[:_UPLOADS_PER_TICK]:
                if self._stop.is_set():
                    break
                if self.upload(row):
                    uploaded += 1
                if _UPLOAD_SLEEP_S:
                    self._stop.wait(_UPLOAD_SLEEP_S)
            removed = self.prune()
        except Exception as exc:  # noqa: BLE001 - the task must never die
            self._fail(None, exc)
        self.backfill_started = True
        self.last_tick_ts = time.time()
        self.refresh_continuity()
        return {"registered": registered, "uploaded": uploaded, "pruned": removed}

    async def run(self):
        """The single background archive task."""
        consecutive = 0
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.tick)
                consecutive = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never crash the process
                consecutive += 1
                self._fail(None, exc)
            delay = max(1.0, config.RAW_ARCHIVE_INTERVAL_S) * min(
                2 ** consecutive, 8)
            await asyncio.sleep(delay)

    # -- reporting ----------------------------------------------------------
    def refresh_continuity(self):
        report = archive_continuity()
        self._continuity = report
        self._continuity_ts = time.monotonic()
        return report

    def continuity(self):
        """Cached continuity: `Engine.status()` reads it from the event loop."""
        if (self._continuity is None
                or time.monotonic() - self._continuity_ts > _CONTINUITY_TTL_S):
            try:
                return self.refresh_continuity()
            except Exception:  # noqa: BLE001 - status must always answer
                return self._continuity or {}
        return self._continuity

    def status(self):
        """The continuity block plus how the archive itself is behaving."""
        report = dict(self.continuity())
        report.update({
            "uploaded_segments": self.uploaded,
            "verified_segments": self.verified,
            "pruned_segments": self.pruned,
            "failures": self.failures,
            "event_failures": self.event_failures,
            "last_error": self.last_error,
            "last_tick_ts": self.last_tick_ts,
            "free_bytes": self.free_bytes(),
        })
        return report
