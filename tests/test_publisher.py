"""Stdlib-only tests for publisher/snapshot_publisher.py. No real Prometheus, no real S3 — the
sanitize/build path is pure, and poll_instant/poll_range/upload_snapshot take their fetcher/
client as arguments precisely so tests can supply fakes instead."""

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "publisher"))

from snapshot_publisher import (  # noqa: E402 - path setup must run first
    build_snapshot,
    poll_instant,
    poll_range,
    sanitize_series,
    upload_snapshot,
)


class SanitizeSeriesTest(unittest.TestCase):
    def test_drops_series_with_a_hostname_like_value_not_in_the_map(self):
        # `instance` (host part, port stripped before lookup) has no map entry -> dropped; a
        # second series with a mapped instance survives untouched.
        sanitize_map = {"web-host-02.example.test": "NODE-2"}
        rows = [
            {"metric": {"instance": "web-host-01.example.test:9100"}, "value": [0, "1"]},
            {"metric": {"instance": "web-host-02.example.test:9100"}, "value": [0, "2"]},
        ]
        kept, dropped = sanitize_series(rows, sanitize_map)
        self.assertEqual(dropped, 1)
        self.assertEqual(kept, [{"metric": {"instance": "NODE-2"}, "value": [0, "2"]}])
        blob = json.dumps(kept)
        self.assertNotIn("web-host-01", blob)
        self.assertNotIn("web-host-02", blob)
        self.assertNotIn("example.test", blob)

    def test_drops_series_with_an_ipv4_value_not_in_the_map(self):
        # 203.0.113.0/24 is the RFC 5737 documentation range — never a real address.
        sanitize_map = {"job": "job-1"}
        rows = [{"metric": {"job": "job", "instance": "203.0.113.5:9100"}, "value": [0, "1"]}]
        kept, dropped = sanitize_series(rows, sanitize_map)
        self.assertEqual(dropped, 1)
        self.assertEqual(kept, [])

    def test_mapped_values_appear_only_as_their_generic_label(self):
        # instance is looked up by host only (see below) — the map key has no port.
        sanitize_map = {"catalog-apps": "GRP-1", "alpha": "GRP-1-01", "web-host-01.example.test": "NODE-1"}
        rows = [{"metric": {"group": "catalog-apps", "name": "alpha", "instance": "web-host-01.example.test:9100"}, "value": [0, "9"]}]
        kept, dropped = sanitize_series(rows, sanitize_map)
        self.assertEqual(dropped, 0)
        self.assertEqual(kept[0]["metric"], {"group": "GRP-1", "name": "GRP-1-01", "instance": "NODE-1"})
        blob = json.dumps(kept)
        self.assertNotIn("catalog-apps", blob)
        self.assertNotIn("web-host-01", blob)

    def test_instance_port_is_stripped_before_lookup_and_never_appears_in_the_output(self):
        sanitize_map = {"web-host-01.example.test": "NODE-1"}
        rows = [{"metric": {"instance": "web-host-01.example.test:9100"}, "value": [0, "1"]}]
        kept, dropped = sanitize_series(rows, sanitize_map)
        self.assertEqual(dropped, 0)
        self.assertEqual(kept[0]["metric"]["instance"], "NODE-1")
        self.assertNotIn(":", kept[0]["metric"]["instance"])
        blob = json.dumps(kept)
        self.assertNotIn("9100", blob)

    def test_device_mountpoint_fstype_and_job_are_stripped_outright_never_mapped(self):
        # No entry for any of these values in the map — proves they're dropped as keys, not
        # merely "would be dropped if unmapped": the series survives because they're never looked up.
        sanitize_map = {"web-host-01.example.test": "NODE-1"}
        rows = [{
            "metric": {
                "instance": "web-host-01.example.test:9100",
                "job": "pve_node_exporter",
                "device": "nfs-host.example.test:/export/shared",
                "mountpoint": "/mnt/pve/shared-nfs",
                "fstype": "nfs4",
            },
            "value": [0, "1"],
        }]
        kept, dropped = sanitize_series(rows, sanitize_map)
        self.assertEqual(dropped, 0)
        self.assertEqual(kept[0]["metric"], {"instance": "NODE-1"})
        blob = json.dumps(kept)
        for leaked in ("nfs-host.example.test", "/export/shared", "/mnt/pve/shared-nfs", "nfs4", "pve_node_exporter"):
            self.assertNotIn(leaked, blob)

    def test_an_nfs_style_device_value_leaves_no_trace_even_when_it_looks_like_a_hostname(self):
        rows = [{"metric": {"device": "nfs-host.example.test:/export/shared"}, "value": [0, "1"]}]
        kept, dropped = sanitize_series(rows, {})
        self.assertEqual(dropped, 0)
        self.assertEqual(kept, [{"metric": {}, "value": [0, "1"]}])
        self.assertNotIn("nfs-host", json.dumps(kept))

    def test_range_rows_keep_their_numeric_values_untouched(self):
        sanitize_map = {"web-host-01.example.test": "NODE-1"}
        rows = [{"metric": {"instance": "web-host-01.example.test:9100"}, "values": [[0, "1"], [30, "2"]]}]
        kept, dropped = sanitize_series(rows, sanitize_map)
        self.assertEqual(dropped, 0)
        self.assertEqual(kept[0]["values"], [[0, "1"], [30, "2"]])


class FakeFetch:
    """Answers query/query_range URLs from a fixed table, keyed on the PromQL `query` param —
    stands in for urllib.request.urlopen so no test needs a real Prometheus."""

    def __init__(self, answers):
        self.answers = answers
        self.urls: list[str] = []

    def __call__(self, url: str):
        self.urls.append(url)
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(url)
        query = parse_qs(parsed.query)["query"][0]
        result = self.answers.get(query, [])
        return {"status": "success", "data": {"resultType": "vector", "result": result}}


class PollTest(unittest.TestCase):
    def test_poll_instant_keys_output_by_the_allow_list_key_not_the_expression(self):
        queries = {"nodeCpu": "up{job=\"pve_node_exporter\"}"}
        sanitize_map = {"web-host-01.example.test": "NODE-1"}
        fetch = FakeFetch({queries["nodeCpu"]: [{"metric": {"job": "pve_node_exporter", "instance": "web-host-01.example.test:9100"}, "value": [0, "1"]}]})
        out, dropped = poll_instant(fetch, "http://prom.example.test:9090", queries, sanitize_map)
        self.assertEqual(dropped, 0)
        # job is stripped outright (never mapped); instance is mapped by host only.
        self.assertEqual(out["nodeCpu"]["result"][0]["metric"], {"instance": "NODE-1"})
        self.assertIn("/api/v1/query?", fetch.urls[0])

    def test_poll_range_counts_a_dropped_series_and_still_returns_the_rest(self):
        queries = {"nodeCpu": "up{job=\"pve_node_exporter\"}"}
        sanitize_map = {"web-host-01.example.test": "NODE-1"}
        fetch = FakeFetch({queries["nodeCpu"]: [
            {"metric": {"job": "pve_node_exporter", "instance": "web-host-01.example.test:9100"}, "values": [[0, "1"]]},
            {"metric": {"job": "pve_node_exporter", "instance": "unmapped-host.example.test:9100"}, "values": [[0, "2"]]},
        ]})
        out, dropped = poll_range(fetch, "http://prom.example.test:9090", queries, sanitize_map, 30, 30)
        self.assertEqual(dropped, 1)
        self.assertEqual(len(out["nodeCpu"]["result"]), 1)
        self.assertIn("/api/v1/query_range?", fetch.urls[0])

    def test_a_failed_query_raises_rather_than_publishing_a_silent_empty_result(self):
        fetch = lambda url: {"status": "error", "error": "forced failure"}  # noqa: E731
        with self.assertRaises(RuntimeError):
            poll_instant(fetch, "http://prom.example.test:9090", {"nodeCpu": "up"}, {})


class SnapshotBuildTest(unittest.TestCase):
    def test_build_snapshot_carries_a_timestamp_and_the_drop_count(self):
        snapshot = build_snapshot({"a": {"result": []}}, {"a": {"result": []}}, dropped=3)
        self.assertIn("generated_at", snapshot)
        self.assertEqual(snapshot["meta"]["dropped_series"], 3)
        self.assertEqual(snapshot["instant"], {"a": {"result": []}})
        self.assertEqual(snapshot["range"], {"a": {"result": []}})


class FakeS3Client:
    def __init__(self):
        self.calls: list[dict] = []

    def put_object(self, **kwargs):
        self.calls.append(kwargs)


class UploadSnapshotTest(unittest.TestCase):
    def test_uploads_with_a_one_second_cache_control_and_json_content_type(self):
        client = FakeS3Client()
        upload_snapshot("wall-public", "data/snapshot.json", {"generated_at": "now"}, client=client)
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["Bucket"], "wall-public")
        self.assertEqual(call["Key"], "data/snapshot.json")
        self.assertEqual(call["CacheControl"], "max-age=1")
        self.assertEqual(call["ContentType"], "application/json")
        self.assertEqual(json.loads(call["Body"]), {"generated_at": "now"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
