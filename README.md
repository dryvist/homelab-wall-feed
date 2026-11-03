# homelab-wall-feed

The read-only data gateway behind [homelab-wall](https://github.com/dryvist/homelab-wall):
native nginx config that exposes a small allowlist of backend read endpoints on
the wall's own origin. It has no code of its own and holds no credentials.

## Endpoints

| Path | Proxies to | Methods |
| --- | --- | --- |
| `/api/prom/query` | Prometheus `/api/v1/query` | GET, HEAD |
| `/api/prom/query_range` | Prometheus `/api/v1/query_range` | GET, HEAD |
| `/api/words` | ClickHouse `/wall/words` | GET, HEAD |
| `/api/llm/recent` | ClickHouse `/wall/recent` | GET, HEAD |

Any other `/api/` path returns 404, and any other method returns 403. The
browser's `Cookie` and `Authorization` headers are stripped before the request
reaches a backend. Responses carry `Cache-Control: no-store`, and backend
`Set-Cookie` headers are dropped. The ClickHouse routes add the
`X-ClickHouse-User` and `X-ClickHouse-Key` headers of a read-only ClickHouse
user from a file the deployer supplies (see below); the Prometheus routes never
carry them.

## Installation

Each release attaches `homelab-wall-feed-nginx.tar.gz` and its `.sha256`.
Put both files from it in the nginx config directory (so the relative
`include wall-feed-proxy.conf` resolves). In the same directory, create
`wall-feed-clickhouse-auth.conf` with the ClickHouse credentials. It is not
part of the release and is never committed anywhere; the deployer renders it
from its secret store:

```nginx
proxy_set_header X-ClickHouse-User wall_reader;
proxy_set_header X-ClickHouse-Key "<password of wall_reader>";
```

Then define both upstreams and include the gateway in the wall's server block:

```nginx
upstream prometheus { server prometheus.example.internal:9090; }
upstream clickhouse { server clickhouse.example.internal:8123; }

server {
    # ... static site ...
    include wall-feed.conf;
}
```

The upstream names and the auth file name are the only contract. The deployer
owns the addresses and the credential.

## Usage

```sh
curl 'https://wall.example.internal/api/prom/query?query=up'
curl 'https://wall.example.internal/api/words'
curl 'https://wall.example.internal/api/llm/recent'
```

## Develop

```sh
nix shell nixpkgs#nginx nixpkgs#python3 -c python3 tests/test_gateway.py -v
```

The test runs real nginx in front of a stdlib stub backend.

## License

Apache-2.0. See [LICENSE](LICENSE).
