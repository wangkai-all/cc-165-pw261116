"""端到端测试:HTTP 服务全流程(生成档案→扫描→检索→抽取→比较→下载→预览 CSP)。"""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from warcview import fixtures
from warcview.server import App, make_handler


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = App(os.path.join(cls.tmp.name, "data"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, raw=False):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers=headers)
        resp = urllib.request.urlopen(r)
        payload = resp.read()
        if raw:
            return resp, payload
        return resp, json.loads(payload.decode())

    def wait_scan(self, archive_id, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, a = self.req("GET", f"/api/archives/{archive_id}")
            if a["scan_status"] in ("done", "error", "paused") and not a["scanning"]:
                return a
            time.sleep(0.1)
        self.fail("scan did not finish")

    def test_full_flow(self):
        # 1. 生成合成档案
        _, arch = self.req("POST", "/api/fixtures")
        aid = arch["id"]
        self.assertEqual(arch["scan_status"], "pending")
        # 档案只读
        mode = os.stat(arch["path"]).st_mode
        self.assertFalse(mode & 0o222, "archive must be read-only")

        # 2. 扫描
        self.req("POST", f"/api/archives/{aid}/scan", {})
        arch = self.wait_scan(aid)
        self.assertEqual(arch["scan_status"], "done")
        self.assertEqual(arch["record_count"], fixtures.EXPECTED_OK)
        self.assertEqual(arch["error_count"], fixtures.EXPECTED_ERRORS)

        # 3. URL 列表
        _, urls = self.req("GET", f"/api/archives/{aid}/urls")
        url_map = {u["url"]: u for u in urls["urls"]}
        self.assertIn(fixtures.PRODUCTS_URL, url_map)

        # 4. 记录检索(时间范围)
        _, recs = self.req(
            "GET", f"/api/archives/{aid}/records?url=products"
            "&from=2024-01-01T00:00:00Z&to=2024-03-15T00:00:00Z")
        self.assertEqual(recs["total"], 3)

        # 5. 抽取任务
        _, run = self.req("POST", "/api/runs", {
            "archive_id": aid, "url": fixtures.PRODUCTS_URL,
            "time_from": "2024-01-01T00:00:00Z", "time_to": "2024-06-01T00:00:00Z",
            "rules": {"title": True, "tables": True,
                      "fields": [{"name": "count", "pattern": r"共 (\d+) 种商品"}]}})
        self.assertEqual(run["run"]["result_count"], 5)
        results = run["results"]
        self.assertTrue(all(r["capture_id"] and r["digest"]
                            and r["file_offset"] is not None for r in results))
        failed = [r for r in results if r["error"]]
        self.assertEqual(len(failed), 1)
        self.assertIn("missing reference", failed[0]["error"])

        # 6. 重跑
        run_id = run["run"]["id"]
        _, again = self.req("POST", f"/api/runs/{run_id}/rerun")
        self.assertEqual(again["run"]["executions"], 2)
        self.assertEqual(len(again["results"]), 5)

        # 7. 下载 CSV / JSON
        resp, csv_data = self.req("GET", f"/api/runs/{run_id}/download?format=csv",
                                  raw=True)
        self.assertIn("text/csv", resp.headers["Content-Type"])
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn("capture_id".encode(), csv_data)
        _, json_data = self.req("GET", f"/api/runs/{run_id}/download?format=json",
                                raw=True)
        parsed = json.loads(json_data.decode())
        self.assertEqual(len(parsed["results"]), 5)

        # 8. 比较两个版本(v1 vs v3)
        _, all_recs = self.req(
            "GET", f"/api/archives/{aid}/records?url=products&type=response&limit=50")
        by_id = {r["record_id"]: r for r in all_recs["records"]}
        v1, v3 = by_id[fixtures.RID["v1"]], by_id[fixtures.RID["v3"]]
        _, cmp_out = self.req("POST", "/api/compare", {
            "record_ids": [v1["id"], v3["id"]],
            "rules": {"title": True, "tables": True}})
        self.assertTrue(cmp_out["diff"]["title"]["changed"])

        # 9. 预览:CSP 必须禁止外部加载
        resp, _ = self.req("GET", f"/api/records/{v1['id']}/preview", raw=True)
        csp = resp.headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'none'", csp)

        # 10. revisit 内容解析
        _, rev = self.req("GET", f"/api/archives/{aid}/records?type=revisit&limit=10")
        rev_ok = [r for r in rev["records"]
                  if r["record_id"] == fixtures.RID["revisit_ok"]][0]
        _, content = self.req("GET", f"/api/records/{rev_ok['id']}/content")
        self.assertTrue(content["ok"])
        self.assertEqual(content["resolved_from"], fixtures.RID["v2"])
        self.assertIn("产品价格表", content["text"])

        # 11. 缺失引用的 revisit:错误可见,不静默
        rev_missing = [r for r in rev["records"]
                       if r["record_id"] == fixtures.RID["revisit_missing"]][0]
        _, content = self.req("GET", f"/api/records/{rev_missing['id']}/content")
        self.assertFalse(content["ok"])
        self.assertIn("missing reference", content["error"])

        # 12. 扫描完成后档案哈希不变
        import hashlib
        h = hashlib.sha256()
        with open(arch["path"], "rb") as f:
            h.update(f.read())
        self.assertEqual(h.hexdigest(), arch["sha256"])


if __name__ == "__main__":
    unittest.main()
