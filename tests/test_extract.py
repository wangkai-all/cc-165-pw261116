"""抽取引擎测试:解码、字符集容错、revisit 解析、抽取规则、比较、任务执行。"""
import json
import os
import tempfile
import unittest

from warcview import engine, fixtures
from warcview.extract import (decode_text, diff_fields, extract_fields,
                              parse_http_message)
from warcview.indexer import Indexer, scan_archive


class DecodeTest(unittest.TestCase):
    def test_charset_declared_ok(self):
        text, cs, err = decode_text("Café".encode("iso-8859-1"),
                                    "text/html; charset=iso-8859-1")
        self.assertEqual(text, "Café")
        self.assertIsNone(err)

    def test_charset_declared_wrong_falls_back_with_error(self):
        payload = "Café".encode("iso-8859-1")  # 非法 utf-8
        text, cs, err = decode_text(payload, "text/html; charset=utf-8")
        self.assertEqual(text, "Café")       # 数据保留
        self.assertIsNotNone(err)            # 但错误被明确标记

    def test_meta_charset_detection(self):
        payload = ("<html><head><meta charset='iso-8859-1'></head>"
                   "<body>Café</body></html>").encode("iso-8859-1")
        text, cs, err = decode_text(payload, "text/html")
        self.assertIn("Café", text)

    def test_http_chunked_and_gzip(self):
        import zlib
        body = b"<html><title>t</title></html>"
        c = zlib.compressobj(6, zlib.DEFLATED, 31)
        gz = c.compress(body) + c.flush()
        wire = (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                b"Content-Encoding: gzip\r\nTransfer-Encoding: chunked\r\n\r\n"
                + f"{len(gz):x}\r\n".encode() + gz + b"\r\n0\r\n\r\n")
        status, headers, payload = parse_http_message(wire)
        self.assertEqual(status, "HTTP/1.1 200 OK")
        from warcview.extract import decode_content_encoding
        decoded, err = decode_content_encoding(payload, headers)
        self.assertIsNone(err)
        self.assertEqual(decoded, body)


class ExtractTest(unittest.TestCase):
    def test_title_and_tables(self):
        fields = extract_fields(fixtures.PRODUCTS_V1.decode("utf-8"),
                                {"title": True, "tables": True})
        self.assertEqual(fields["title"], "产品价格表")
        self.assertEqual(len(fields["tables"]), 1)
        rows = fields["tables"][0]["rows"]
        self.assertEqual(rows[0], ["名称", "价格", "库存"])
        self.assertEqual(len(rows), 4)

    def test_custom_regex_fields(self):
        fields = extract_fields(fixtures.PRODUCTS_V1.decode("utf-8"),
                                {"fields": [{"name": "count",
                                             "pattern": r"共 (\d+) 种商品"}]})
        self.assertEqual(fields["fields"]["count"], ["3"])

    def test_diff(self):
        a = extract_fields(fixtures.PRODUCTS_V1.decode("utf-8"),
                           {"title": True, "tables": True})
        b = extract_fields(fixtures.PRODUCTS_V3.decode("utf-8"),
                           {"title": True, "tables": True})
        d = diff_fields(a, b)
        self.assertTrue(d["title"]["changed"])
        added = d["tables"][0]["added"]
        removed = d["tables"][0]["removed"]
        self.assertIn(["西瓜", "5.50", "30"], added)
        self.assertIn(["苹果", "3.00", "120"], removed)
        self.assertIn(["苹果", "3.20", "100"], added)


class EngineTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.archive_path = os.path.join(cls.tmp.name, "sample.warc.gz")
        fixtures.write_fixture(cls.archive_path)
        cls.indexer = Indexer(os.path.join(cls.tmp.name, "index.sqlite3"))
        cls.archive_id = cls.indexer.create_archive(
            "sample.warc.gz", cls.archive_path, "gz",
            os.path.getsize(cls.archive_path), "x")
        scan_archive(cls.indexer, cls.archive_id)
        cls.archive = cls.indexer.get_archive(cls.archive_id)

    @classmethod
    def tearDownClass(cls):
        cls.indexer.close()
        cls.tmp.cleanup()

    def record(self, rid):
        row = self.indexer.find_by_record_id(self.archive_id, fixtures.RID[rid])
        self.assertIsNotNone(row, rid)
        return row


class PayloadTest(EngineTestBase):
    def test_response_payload(self):
        info = engine.decode_record_text(self.indexer, self.archive, self.record("v1"))
        self.assertTrue(info["ok"])
        self.assertIn("产品价格表", info["text"])
        self.assertIsNone(info["charset_error"])

    def test_revisit_resolves_to_original(self):
        info = engine.decode_record_text(self.indexer, self.archive,
                                         self.record("revisit_ok"))
        self.assertTrue(info["ok"])
        self.assertEqual(info["resolved_from"], fixtures.RID["v2"])
        self.assertIn("产品价格表", info["text"])

    def test_revisit_missing_reference(self):
        info = engine.decode_record_text(self.indexer, self.archive,
                                         self.record("revisit_missing"))
        self.assertFalse(info["ok"])
        self.assertIn("missing reference", info["error"])

    def test_charset_declared_correctly(self):
        info = engine.decode_record_text(self.indexer, self.archive,
                                         self.record("about_ok"))
        self.assertTrue(info["ok"])
        self.assertIsNone(info["charset_error"])
        self.assertIn("Café", info["text"])

    def test_charset_failure_flagged_not_dropped(self):
        info = engine.decode_record_text(self.indexer, self.archive,
                                         self.record("about_bad"))
        self.assertTrue(info["ok"])
        self.assertIsNotNone(info["charset_error"])  # 明确标记
        self.assertIn("Café", info["text"])          # 数据仍在

    def test_gzip_chunked_body(self):
        info = engine.decode_record_text(self.indexer, self.archive,
                                         self.record("data"))
        self.assertTrue(info["ok"])
        self.assertIsNone(info["ce_error"])
        self.assertIn("访问统计", info["text"])


class RunTest(EngineTestBase):
    def test_run_time_range_and_result_fields(self):
        run_id = self.indexer.create_run(
            self.archive_id, fixtures.PRODUCTS_URL,
            "2024-01-01T00:00:00Z", "2024-03-15T00:00:00Z",
            {"title": True, "tables": True})
        engine.execute_run(self.indexer, run_id)
        results = self.indexer.run_results(run_id)
        self.assertEqual(len(results), 3)  # v1, v2, revisit_ok
        for r in results:
            self.assertTrue(r["capture_id"])       # 捕获编号
            self.assertTrue(r["digest"])           # 正文指纹
            self.assertIsNotNone(r["file_offset"])  # 来源偏移
        # 相同正文(v1/v2/revisit)指纹一致,时间各自保留
        digests = {r["digest"] for r in results}
        self.assertEqual(len(digests), 1)
        times = [r["timestamp"] for r in results]
        self.assertEqual(times, ["2024-01-01T10:00:00Z", "2024-02-01T10:00:00Z",
                                 "2024-03-01T10:00:00Z"])
        fields = [json.loads(r["fields_json"]) for r in results]
        self.assertTrue(all(f.get("title") == "产品价格表" for f in fields))

    def test_run_includes_failed_captures_with_error(self):
        run_id = self.indexer.create_run(
            self.archive_id, fixtures.PRODUCTS_URL,
            "2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z",
            {"title": True, "tables": True})
        engine.execute_run(self.indexer, run_id)
        results = self.indexer.run_results(run_id)
        # v1, v2, revisit_ok, v3, revisit_missing = 5 条,缺失引用的不丢弃
        self.assertEqual(len(results), 5)
        failed = [r for r in results if r["error"]]
        self.assertEqual(len(failed), 1)
        self.assertIn("missing reference", failed[0]["error"])
        self.assertEqual(failed[0]["capture_id"], fixtures.RID["revisit_missing"])

    def test_rerun_replaces_results(self):
        run_id = self.indexer.create_run(
            self.archive_id, fixtures.PRODUCTS_URL,
            "2024-01-01T00:00:00Z", "2024-03-15T00:00:00Z",
            {"title": True, "tables": True})
        engine.execute_run(self.indexer, run_id)
        engine.execute_run(self.indexer, run_id)  # 重跑
        results = self.indexer.run_results(run_id)
        self.assertEqual(len(results), 3)
        run = self.indexer.get_run(run_id)
        self.assertEqual(run["executions"], 2)

    def test_compare_versions(self):
        v1 = self.record("v1")
        v3 = self.record("v3")
        out = engine.compare_records(self.indexer, self.archive,
                                     v1["id"], v3["id"],
                                     {"title": True, "tables": True})
        self.assertTrue(out["diff"]["title"]["changed"])
        self.assertEqual(out["diff"]["title"]["from"], "产品价格表")
        self.assertEqual(out["diff"]["title"]["to"], "产品价格表(2024 春季)")
        added = out["diff"]["tables"][0]["added"]
        self.assertIn(["西瓜", "5.50", "30"], added)


if __name__ == "__main__":
    unittest.main()
