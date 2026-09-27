# homelab-wall-feed

Two ways to hand [homelab-wall](https://github.com/dryvist/homelab-wall) its data:

- `nginx/`: a read-only gateway — native nginx config exposing a small allowlist
  of backend read endpoints on the wall's own origin. No code of its own, holds
  no credentials. For an internal-only deployment.
- `publisher/`: a small Python program that polls Prometheus, sanitizes every
  label value, and publishes one JSON snapshot to S3, for a public deployment
  where nothing may call back into the source network. See
  [Snapshot publisher](#snapshot-publisher) below.

## Endpoints

| Path | Proxies to | Methods |
| --- | --- | --- |
| `/api/prom/query` | Prometheus `/api/v1/query` | GET, HEAD |
| `/api/prom/query_range` | Prometheus `/api/v1/query_range` | GET, HEAD |

Any other `/api/` path returns 404, and any other method returns 403. The
browser's `Cookie` and `Authorization` headers are stripped before the request
reaches a backend. Responses carry `Cache-Control: no-store`, and backend
`Set-Cookie` headers are dropped.

## Installation

Each release attaches `homelab-wall-feed-nginx.tar.gz` and its `.sha256`.
Put both files from it in the nginx config directory (so the relative
`include wall-feed-proxy.conf` resolves). Then define the upstream and include
the gateway in the wall's server block:

```nginx
upstream prometheus { server prometheus.example.internal:9090; }

server {
    # ... static site ...
    include wall-feed.conf;
}
```

The upstream name is the only contract. The deployer owns the address.

## Usage

```sh
curl 'https://wall.example.internal/api/prom/query?query=up'
```

## Develop

```sh
nix shell nixpkgs#nginx nixpkgs#python3 -c python3 tests/test_gateway.py -v
python3 -W error::ResourceWarning tests/test_publisher.py -v
```

The gateway test runs real nginx in front of a stdlib stub backend. The
publisher test needs nothing beyond the standard library (its S3/Prometheus
calls are injected as fakes).

## Snapshot publisher

`publisher/snapshot_publisher.py`: stdlib only, except the S3 upload
(`boto3`, imported lazily). Every 2s it runs the allow-listed instant
queries; every 30s the same allow-list as range queries. The allow-list is
one `{key: PromQL}` JSON file — homelab-wall's `scripts/gen-queries.mjs`
generates it from `site/lib/queries.js`'s `Q`, so both repos read the same
source.

The `device`, `mountpoint`, `fstype` and `job` labels are stripped outright
— never mapped, never published. `instance`'s `:port` suffix is stripped
before lookup, so a published `instance` value never contains `:`. Every
other label VALUE on every returned series is looked up in a sanitize map
(a flat `{real value: generic label}` JSON file); a series with any
remaining value not in the map is dropped, never passed through partially
sanitized — fail closed. The dropped-series count is recorded in the
snapshot's own `meta.dropped_series`, never silently discarded.

The result is written as one JSON object to S3 at `data/snapshot.json` (by
default) with `Cache-Control: max-age=1`, using `boto3`'s default
credential chain — this script never fetches or holds a credential itself.

```sh
python3 publisher/snapshot_publisher.py \
  --prom-url http://prometheus.example.internal:9090 \
  --queries-file queries.json \
  --sanitize-map sanitize-map.json \
  --bucket wall-public-example
```

Each release also attaches `homelab-wall-feed-publisher.tar.gz` and its
`.sha256`, containing `snapshot_publisher.py` and `requirements.txt`.

## License

Apache-2.0. See [LICENSE](LICENSE).
