# homelab-wall-feed

The read-only data gateway behind [homelab-wall](https://github.com/dryvist/homelab-wall):
native nginx config that exposes a small allowlist of backend read endpoints on
the wall's own origin. It has no code of its own and holds no credentials.

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

Put both files from `nginx/` in the nginx config directory (so the relative
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
```

The test runs real nginx in front of a stdlib stub backend.

## License

Apache-2.0. See [LICENSE](LICENSE).
