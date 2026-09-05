#!/usr/bin/env python
"""Operator connectivity probe for the raw-feed R2 archive.

NOT part of the test suite and NOT run by the application: this is the thing an
operator runs once, after setting the R2 variables on Railway, to confirm that
the credentials work and that the two mechanisms the continuity contract
depends on are real in the live bucket:

  * a single PUT returns an ETag equal to the md5 of the body -- which is what
    `RawArchive.upload` compares before a segment is ever eligible for pruning;
  * a ranged GET returns 206 with exactly the requested bytes -- which is what
    `GET /api/export/raw/{name}` relies on when it serves a pruned segment.

It writes one small temporary object under `probe/`, reads it back whole and by
range, and deletes it again.  It never touches `raw/`, so it cannot disturb an
archived segment.

Usage (credentials come from the environment; nothing is committed):

    R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \\
    R2_BUCKET=football-bot-raw-feed .venv/bin/python scripts/r2_probe.py

Exit status is 0 when every check passed, 1 otherwise.  No credential value is
printed.
"""
import hashlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app import config  # noqa: E402
from app.archive import EMPTY_SHA256, R2Client  # noqa: E402

BODY = b"football-bot r2 connectivity probe\n" * 64


def main():
    endpoint = config.r2_endpoint()
    if not endpoint or not config.R2_ACCESS_KEY_ID or not config.R2_SECRET_ACCESS_KEY:
        print("R2 is not configured: set R2_ACCOUNT_ID (or R2_ENDPOINT), "
              "R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY.")
        return 1
    client = R2Client(endpoint, config.R2_BUCKET,
                      config.R2_ACCESS_KEY_ID, config.R2_SECRET_ACCESS_KEY)
    key = f"probe/connectivity-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.bin"
    md5 = hashlib.md5(BODY).hexdigest()
    sha256 = hashlib.sha256(BODY).hexdigest()
    print(f"endpoint {endpoint}")
    print(f"bucket   {config.R2_BUCKET}")
    print(f"key      {key}  ({len(BODY)} bytes)")

    failures = []

    def check(label, ok, detail=""):
        print(f"  [{'ok' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    url = client.url_for(key)
    headers = client.signer.sign("PUT", url, sha256,
                                 extra={"content-type": "application/octet-stream"})
    with httpx.Client(timeout=60.0) as http:
        put = http.put(url, headers=headers, content=BODY)
        check("PUT", put.status_code in (200, 201), f"status {put.status_code}")
        etag = (put.headers.get("ETag") or "").strip('"')
        check("ETag == md5(body)", etag == md5, f"etag {etag or 'missing'}")

        head = client.head(key)
        check("HEAD", head.status_code == 200, f"status {head.status_code}")
        check("HEAD content-length == local size",
              head.headers.get("Content-Length") == str(len(BODY)),
              f"length {head.headers.get('Content-Length')}")

        get_headers = client.signer.sign("GET", url)
        whole = http.get(url, headers=get_headers)
        check("GET", whole.status_code == 200, f"status {whole.status_code}")
        check("GET body sha256 == source",
              hashlib.sha256(whole.content).hexdigest() == sha256)

        ranged_headers = client.signer.sign("GET", url, extra={"range": "bytes=0-9"})
        ranged = http.get(url, headers=ranged_headers)
        check("Range GET -> 206", ranged.status_code == 206,
              f"status {ranged.status_code}")
        check("Range GET returns the requested bytes",
              ranged.content == BODY[:10], f"{len(ranged.content)} bytes")

        delete_headers = client.signer.sign("DELETE", url, EMPTY_SHA256)
        deleted = http.request("DELETE", url, headers=delete_headers)
        check("DELETE probe object", deleted.status_code in (200, 204),
              f"status {deleted.status_code}")

    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("\nAll checks passed. The archive can upload, verify and serve ranges.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
