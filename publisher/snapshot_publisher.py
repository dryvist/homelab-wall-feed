#!/usr/bin/env python3
"""Publishes a sanitized Prometheus snapshot for the public wall (homelab-wall's snapshot
source, site/lib/prom.js). Stdlib only, except the S3 upload (boto3, imported lazily so the
rest of this module — and its tests — need nothing beyond the standard library).

Every `--instant-interval-seconds` (default 2s) it runs every query in the allow-list
(`--queries-file`, the {key: PromQL} JSON homelab-wall's scripts/gen-queries.mjs emits) as an
instant query; every `--range-interval-seconds` (default 30s) it also runs the same allow-list
as a range query. The `device`/`mountpoint`/`fstype`/`job` labels are stripped outright (never
mapped, never emitted); `instance`'s `:port` suffix is stripped before lookup. Every other label
VALUE on every returned series is looked up in the sanitize map (`--sanitize-map`, a flat
{real value: generic label} JSON file) — a series with any value not in the map is DROPPED,
never passed through unmapped (fail closed; the drop count is recorded in the snapshot's own
meta, never silent). The result is written as one JSON object to S3
(`--bucket`/`--key`) with Cache-Control: max-age=1, using boto3's default credential chain —
this script never fetches or holds a credential itself.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

Series = dict[str, Any]
Queries = dict[str, str]
SanitizeMap = dict[str, str]

# These label keys never leave the publisher: they carry no operator-approved generic form
# (a filesystem's device/mountpoint/fstype, or the fixed node-exporter job literal), so they are
# dropped outright rather than run through the sanitize map. A page that needs per-node storage
# aggregates it from the remaining `instance` label instead of reading these.
STRIPPED_LABELS = frozenset({"device", "mountpoint", "fstype", "job"})


def _lookup_value(label: str, value: str) -> str:
    """The string actually looked up in the sanitize map for this label. `instance` arrives as
    `host:port` (Prometheus's own scrape-target format); only the host part is ever mapped or
    kept — the port is dropped outright, so a sanitized instance value never contains ':'."""
    if label == "instance":
        return value.split(":", 1)[0]
    return value


def sanitize_series(rows: list[Series], sanitize_map: SanitizeMap) -> tuple[list[Series], int]:
    """Maps every remaining label value on every row through sanitize_map; a row with any
    unmapped value is dropped rather than passed through partially sanitized. STRIPPED_LABELS are
    removed first, without ever being looked up. Only `metric` is touched — `value`/`values` are
    numeric time-series data, never label values."""
    kept: list[Series] = []
    dropped = 0
    for row in rows:
        metric = row.get("metric", {})
        sanitized: dict[str, str] = {}
        ok = True
        for label, value in metric.items():
            if label in STRIPPED_LABELS:
                continue
            mapped = sanitize_map.get(_lookup_value(label, value))
            if mapped is None:
                ok = False
                break
            sanitized[label] = mapped
        if not ok:
            dropped += 1
            continue
        kept.append({**row, "metric": sanitized})
    return kept, dropped


# fetch(url) -> parsed JSON body; swapped out in tests so no test needs a real Prometheus.
Fetcher = Callable[[str], dict[str, Any]]


def urlopen_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as res:  # noqa: S310 - fixed http(s) prom_url only
        return json.load(res)


def prom_result(fetch: Fetcher, base_url: str, path: str, params: dict[str, Any]) -> list[Series]:
    url = f"{base_url.rstrip('/')}/api/v1/{path}?{urllib.parse.urlencode(params)}"
    body = fetch(url)
    if body.get("status") != "success":
        raise RuntimeError(f"{path} {params.get('query')!r} failed: {body.get('error', 'unknown error')}")
    return body["data"]["result"]


def poll_instant(fetch: Fetcher, base_url: str, queries: Queries, sanitize_map: SanitizeMap) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    dropped = 0
    for key, expr in queries.items():
        rows = prom_result(fetch, base_url, "query", {"query": expr})
        kept, d = sanitize_series(rows, sanitize_map)
        dropped += d
        out[key] = {"result": kept}
    return out, dropped


def poll_range(fetch: Fetcher, base_url: str, queries: Queries, sanitize_map: SanitizeMap, minutes: float, step_seconds: float) -> tuple[dict[str, Any], int]:
    end = time.time()
    start = end - minutes * 60
    out: dict[str, Any] = {}
    dropped = 0
    for key, expr in queries.items():
        rows = prom_result(fetch, base_url, "query_range", {"query": expr, "start": start, "end": end, "step": step_seconds})
        kept, d = sanitize_series(rows, sanitize_map)
        dropped += d
        out[key] = {"result": kept}
    return out, dropped


def build_snapshot(instant: dict[str, Any], range_: dict[str, Any], dropped: int) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "meta": {"dropped_series": dropped},
        "instant": instant,
        "range": range_,
    }


def upload_snapshot(bucket: str, key: str, snapshot: dict[str, Any], client=None) -> None:
    if client is None:
        import boto3  # lazy: only the real upload path needs it, not the sanitize/build logic
        client = boto3.client("s3")
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(snapshot).encode(),
        ContentType="application/json",
        CacheControl="max-age=1",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prom-url", required=True, help="Prometheus-compatible base URL, e.g. http://prometheus.internal:9090")
    p.add_argument("--queries-file", required=True, help="{key: PromQL} JSON allow-list (homelab-wall's site/queries.json)")
    p.add_argument("--sanitize-map", required=True, help="{real label value: generic label} JSON file")
    p.add_argument("--bucket", required=True, help="S3 bucket to publish to")
    p.add_argument("--key", default="data/snapshot.json", help="S3 key (default: data/snapshot.json)")
    p.add_argument("--instant-interval-seconds", type=float, default=2.0)
    p.add_argument("--range-interval-seconds", type=float, default=30.0)
    p.add_argument("--range-minutes", type=float, default=30.0)
    p.add_argument("--range-step-seconds", type=float, default=30.0)
    p.add_argument("--once", action="store_true", help="run a single poll+upload cycle and exit (for a smoke check)")
    return p.parse_args(argv)


def run(args: argparse.Namespace, fetch: Fetcher = urlopen_json, client=None) -> None:
    queries: Queries = json.loads(open(args.queries_file).read())
    sanitize_map: SanitizeMap = json.loads(open(args.sanitize_map).read())

    range_cache: dict[str, Any] = {}
    last_range_at = 0.0
    while True:
        tick_start = time.monotonic()
        instant, instant_dropped = poll_instant(fetch, args.prom_url, queries, sanitize_map)
        range_dropped = 0
        if not range_cache or tick_start - last_range_at >= args.range_interval_seconds:
            range_cache, range_dropped = poll_range(fetch, args.prom_url, queries, sanitize_map, args.range_minutes, args.range_step_seconds)
            last_range_at = tick_start
        snapshot = build_snapshot(instant, range_cache, instant_dropped + range_dropped)
        upload_snapshot(args.bucket, args.key, snapshot, client=client)
        if args.once:
            return
        elapsed = time.monotonic() - tick_start
        time.sleep(max(0.0, args.instant_interval_seconds - elapsed))


def main(argv: list[str] | None = None) -> None:
    run(parse_args(argv))


if __name__ == "__main__":
    main()
