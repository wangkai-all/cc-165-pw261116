"""索引与扫描测试:计数、损坏记录保留、中断恢复、档案不可变。"""
import hashlib
import os
import shutil
import tempfile
import threading
import unittest

from warcview import fixtures
from warcview.indexer import Indexer, scan_archive


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ScanTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive_path = os.path.join(self.tmp.name, "sample.warc.gz")
        fixtures.write_fixture(self.archive_path)
        self.before_hash = sha256_of(self.archive_path)
        self.indexer = Indexer(os.path.join(self.tmp.name, "index.sqlite3"))
        fmt = "gz"
        self.archive_id = self.indexer.create_archive(
            "sample.warc.gz", self.archive_path, fmt,
            os.path.getsize(self.archive_path), self.before_hash)
        self.indexer.update_archive(self.archive_id, path=self.archive_path)

    def tearDown(self):
        self.indexer.close()
        self.tmp.cleanup()

    def records(self):
        rows, total = self.indexer.query_records(self.archive_id, limit=10000)
        self.assertEqual(total, len(rows))
        return rows


class FullScanTest(ScanTestBase):
    def test_full_scan_counts(self):
        arch = scan_archive(self.indexer, self.archive_id)
        self.assertEqual(arch["scan_status"], "done")
        rows = self.records()
        ok = [r for r in rows if r["type"] != "error"]
        errors = [r for r in rows if r["type"] == "error"]
        self.assertEqual(len(ok), fixtures.EXPECTED_OK)
        self.assertEqual(len(errors), fixtures.EXPECTED_ERRORS)

    def test_corrupt_records_keep_offset(self):
        scan_archive(self.indexer, self.archive_id)
        errors = [r for r in self.records() if r["type"] == "error"]
        self.assertEqual(len(errors), fixtures.EXPECTED_ERRORS)
        for e in errors:
            self.assertEqual(e["status"], "corrupt")
            self.assertIsNotNone(e["file_offset"])
            self.assertTrue(e["error"])

    def test_revisit_records_distinguished(self):
        scan_archive(self.indexer, self.archive_id)
        rows = self.records()
        revisits = [r for r in rows if r["type"] == "revisit"]
        responses = [r for r in rows if r["type"] == "response"]
        self.assertEqual(len(revisits), 2)
        self.assertEqual(len(responses), 6)
        for rv in revisits:
            self.assertTrue(rv["refers_to"])
        # 缺失引用的 revisit 指向不存在的记录
        missing = [r for r in revisits if r["refers_to"] == fixtures.MISSING_REF]
        self.assertEqual(len(missing), 1)

    def test_duplicate_body_keeps_separate_timestamps(self):
        scan_archive(self.indexer, self.archive_id)
        rows = [r for r in self.records()
                if r["url"] == fixtures.PRODUCTS_URL and r["type"] == "response"]
        v1 = [r for r in rows if r["timestamp"] == "2024-01-01T10:00:00Z"]
        v2 = [r for r in rows if r["timestamp"] == "2024-02-01T10:00:00Z"]
        self.assertEqual(len(v1), 1)
        self.assertEqual(len(v2), 1)
        self.assertEqual(v1[0]["digest"], v2[0]["digest"])  # 相同正文指纹
        self.assertNotEqual(v1[0]["record_id"], v2[0]["record_id"])  # 各自捕获编号

    def test_archive_file_not_modified(self):
        scan_archive(self.indexer, self.archive_id)
        self.assertEqual(sha256_of(self.archive_path), self.before_hash)


class ResumeTest(ScanTestBase):
    def test_resume_after_interruption(self):
        # 模拟中断:只处理 3 条
        arch = scan_archive(self.indexer, self.archive_id, max_records=3)
        self.assertEqual(arch["scan_status"], "paused")
        partial = self.records()
        self.assertEqual(len(partial), 3)
        # 恢复扫描
        arch = scan_archive(self.indexer, self.archive_id)
        self.assertEqual(arch["scan_status"], "done")
        rows = self.records()
        self.assertEqual(len(rows), fixtures.EXPECTED_OK + fixtures.EXPECTED_ERRORS)
        # 无重复偏移
        offsets = [r["file_offset"] for r in rows]
        self.assertEqual(len(offsets), len(set(offsets)))

    def test_resume_matches_uninterrupted_scan(self):
        scan_archive(self.indexer, self.archive_id, max_records=4)
        scan_archive(self.indexer, self.archive_id)
        rows_resumed = self.records()

        tmp2 = tempfile.TemporaryDirectory()
        try:
            idx2 = Indexer(os.path.join(tmp2.name, "i2.sqlite3"))
            aid2 = idx2.create_archive("s", self.archive_path, "gz",
                                       os.path.getsize(self.archive_path), "x")
            scan_archive(idx2, aid2)
            rows_full, _ = idx2.query_records(aid2, limit=10000)
            idx2.close()
        finally:
            tmp2.cleanup()
        key = lambda r: (r["file_offset"], r["type"], r["record_id"], r["status"])
        self.assertEqual([key(r) for r in rows_resumed],
                         [key(r) for r in rows_full])

    def test_stop_event_pauses_scan(self):
        stop = threading.Event()
        orig_insert = self.indexer.insert_record
        count = {"n": 0}

        def insert_and_stop(row):
            orig_insert(row)
            count["n"] += 1
            if count["n"] >= 2:
                stop.set()

        self.indexer.insert_record = insert_and_stop
        arch = scan_archive(self.indexer, self.archive_id, stop_event=stop)
        self.assertEqual(arch["scan_status"], "paused")
        self.indexer.insert_record = orig_insert
        arch = scan_archive(self.indexer, self.archive_id)
        self.assertEqual(arch["scan_status"], "done")
        self.assertEqual(len(self.records()),
                         fixtures.EXPECTED_OK + fixtures.EXPECTED_ERRORS)

    def test_force_rescan(self):
        scan_archive(self.indexer, self.archive_id)
        arch = scan_archive(self.indexer, self.archive_id, force=True)
        self.assertEqual(arch["scan_status"], "done")
        self.assertEqual(len(self.records()),
                         fixtures.EXPECTED_OK + fixtures.EXPECTED_ERRORS)


class QueryTest(ScanTestBase):
    def test_list_urls(self):
        scan_archive(self.indexer, self.archive_id)
        urls = {u["url"]: u for u in self.indexer.list_urls(self.archive_id)}
        self.assertIn(fixtures.PRODUCTS_URL, urls)
        self.assertEqual(urls[fixtures.PRODUCTS_URL]["captures"], 5)
        self.assertEqual(urls[fixtures.PRODUCTS_URL]["revisits"], 2)
        self.assertEqual(urls[fixtures.ABOUT_URL]["captures"], 2)

    def test_time_range_filter(self):
        scan_archive(self.indexer, self.archive_id)
        rows, total = self.indexer.query_records(
            self.archive_id, url_exact=fixtures.PRODUCTS_URL,
            time_from="2024-01-01T00:00:00Z", time_to="2024-03-15T00:00:00Z")
        self.assertEqual(total, 3)  # v1, v2, revisit_ok
        self.assertEqual([r["timestamp"] for r in rows],
                         ["2024-01-01T10:00:00Z", "2024-02-01T10:00:00Z",
                          "2024-03-01T10:00:00Z"])


if __name__ == "__main__":
    unittest.main()
