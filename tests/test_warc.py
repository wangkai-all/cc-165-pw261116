"""WARC 解析层测试:偏移、损坏记录、重同步、随机访问。"""
import os
import tempfile
import unittest

from warcview import fixtures, warc


class FixtureScanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "sample.warc.gz")
        fixtures.write_fixture(cls.path)
        cls.size = os.path.getsize(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_sniff_format(self):
        self.assertEqual(warc.sniff_format(self.path), "gz")

    def test_first_record_is_warcinfo(self):
        with open(self.path, "rb") as f:
            rec = warc.load_record_at(f, "gz", 0)
        self.assertEqual(rec.headers.get("warc-type"), "warcinfo")
        self.assertEqual(rec.offset, 0)
        self.assertGreater(rec.member_length, 0)

    def test_sequential_records_chain_by_offset(self):
        # 前两条记录必须能通过 end_offset 链式衔接
        with open(self.path, "rb") as f:
            r1 = warc.load_record_at(f, "gz", 0)
            r2 = warc.load_record_at(f, "gz", r1.end_offset)
        self.assertEqual(r2.headers.get("warc-type"), "response")
        self.assertEqual(r2.headers.get("warc-target-uri"), fixtures.PRODUCTS_URL)

    def test_corrupt_member_raises_with_offset(self):
        # 找到第 6 个 member(损坏的 CRC 记录)
        with open(self.path, "rb") as f:
            off = 0
            for _ in range(5):
                rec = warc.load_record_at(f, "gz", off)
                off = rec.end_offset
            with self.assertRaises(warc.WarcError) as ctx:
                warc.load_record_at(f, "gz", off)
        self.assertEqual(ctx.exception.offset, off)
        self.assertIn("CRC", ctx.exception.message)

    def test_resync_finds_valid_member_after_corrupt(self):
        with open(self.path, "rb") as f:
            off = 0
            for _ in range(5):
                rec = warc.load_record_at(f, "gz", off)
                off = rec.end_offset
            nxt = warc.find_next_member(f, off + 1, self.size, "gz")
            self.assertIsNotNone(nxt)
            rec = warc.load_record_at(f, "gz", nxt)
        # 重同步后应读到 products v3
        self.assertEqual(rec.headers.get("warc-record-id"),
                         f"<urn:uuid:{fixtures.RID['v3']}>")

    def test_truncated_tail_member(self):
        # 最后一个 member 截断:读取应报错而不是崩溃
        with open(self.path, "rb") as f:
            # 定位 garbage 之后的有效记录,再往后就是截断 member
            off = 0
            offsets = []
            while off < self.size:
                try:
                    rec = warc.load_record_at(f, "gz", off)
                    offsets.append(off)
                    off = rec.end_offset
                except warc.WarcError:
                    nxt = warc.find_next_member(f, off + 1, self.size, "gz")
                    if nxt is None:
                        break
                    off = nxt
            # 最后一条有效记录是 data;其后是截断 member
            last = offsets[-1]
            rec = warc.load_record_at(f, "gz", last)
            self.assertEqual(rec.headers.get("warc-record-id"),
                             f"<urn:uuid:{fixtures.RID['data']}>")
            with self.assertRaises(warc.WarcError):
                warc.load_record_at(f, "gz", rec.end_offset)

    def test_plain_warc_roundtrip(self):
        # 非压缩 .warc:把所有有效记录拼接成明文档案
        records = []
        with open(self.path, "rb") as f:
            off = 0
            while off < self.size:
                try:
                    rec = warc.load_record_at(f, "gz", off)
                    records.append(rec)
                    off = rec.end_offset
                except warc.WarcError:
                    nxt = warc.find_next_member(f, off + 1, self.size, "gz")
                    if nxt is None:
                        break
                    off = nxt
        plain = os.path.join(self.tmp.name, "plain.warc")
        with open(plain, "wb") as out:
            for rec in records:
                head = rec.version + "\r\n"
                head += "".join(f"{k}: {v}\r\n" for k, v in rec.headers.items())
                out.write(head.encode("iso-8859-1") + b"\r\n" + rec.content + b"\r\n\r\n")
        self.assertEqual(warc.sniff_format(plain), "plain")
        with open(plain, "rb") as f:
            off = 0
            count = 0
            size = os.path.getsize(plain)
            while off < size:
                rec, end = warc.read_plain_record(f, off)
                self.assertTrue(rec.version.startswith("WARC/"))
                off = end
                count += 1
        self.assertEqual(count, len(records))


if __name__ == "__main__":
    unittest.main()
