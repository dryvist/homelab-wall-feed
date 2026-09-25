"""Runs nginx with the gateway config in front of a stub Prometheus and checks
what gets through. Needs `nginx` on PATH; stdlib only otherwise."""

import http.server
import json
import pathlib
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stub(http.server.BaseHTTPRequestHandler):
    """Echoes what reached the backend so tests can assert on it."""

    def do_GET(self):
        body = json.dumps({
            "path": self.path,
            "cookie": self.headers.get("Cookie"),
            "authorization": self.headers.get("Authorization"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "leak=1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, format, *args):  # noqa: A002 - base signature
        pass


class GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), Stub)
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()

        cls.tmp = pathlib.Path(tempfile.mkdtemp())
        for f in (ROOT / "nginx").glob("*.conf"):
            shutil.copy(f, cls.tmp / f.name)
        cls.port = free_port()
        (cls.tmp / "nginx.conf").write_text(f"""
daemon off;
pid {cls.tmp}/nginx.pid;
error_log {cls.tmp}/error.log;
events {{}}
http {{
  access_log off;
  client_body_temp_path {cls.tmp}; proxy_temp_path {cls.tmp};
  fastcgi_temp_path {cls.tmp}; uwsgi_temp_path {cls.tmp}; scgi_temp_path {cls.tmp};
  upstream prometheus {{ server 127.0.0.1:{cls.stub.server_address[1]}; }}
  server {{
    listen 127.0.0.1:{cls.port};
    include {cls.tmp}/wall-feed.conf;
  }}
}}
""")
        subprocess.run(["nginx", "-t", "-p", str(cls.tmp), "-c", str(cls.tmp / "nginx.conf")],
                       check=True, capture_output=True)
        cls.nginx = subprocess.Popen(["nginx", "-p", str(cls.tmp), "-c", str(cls.tmp / "nginx.conf")])
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.nginx.terminate()
        cls.nginx.wait()
        cls.stub.shutdown()
        cls.stub.server_close()
        shutil.rmtree(cls.tmp)

    def call(self, path: str, method: str = "GET",
             headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     headers=headers or {}, data=b"x" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), {}

    def test_query_forwards_path_and_args(self):
        status, headers, body = self.call("/api/prom/query?query=up%7Bjob%3D%22x%22%7D")
        self.assertEqual(status, 200)
        self.assertEqual(body["path"], "/api/v1/query?query=up%7Bjob%3D%22x%22%7D")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertNotIn("Set-Cookie", headers)

    def test_query_range_forwards(self):
        status, _, body = self.call("/api/prom/query_range?query=up&start=1&end=2&step=15")
        self.assertEqual(status, 200)
        self.assertEqual(body["path"], "/api/v1/query_range?query=up&start=1&end=2&step=15")

    def test_browser_credentials_are_stripped(self):
        _, _, body = self.call("/api/prom/query?query=up",
                               headers={"Cookie": "authelia_session=s", "Authorization": "Bearer t"})
        self.assertIsNone(body["cookie"])
        self.assertIsNone(body["authorization"])

    def test_post_is_refused(self):
        self.assertEqual(self.call("/api/prom/query", method="POST")[0], 403)

    def test_other_prometheus_apis_are_not_exposed(self):
        for path in ("/api/v1/query?query=up", "/api/prom/admin/tsdb/snapshot",
                     "/api/prom/series?match[]=up", "/api/prom/query/../targets"):
            with self.subTest(path=path):
                self.assertEqual(self.call(path)[0], 404)


if __name__ == "__main__":
    unittest.main()
