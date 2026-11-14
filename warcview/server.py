"""本地 HTTP 服务:档案上传、扫描控制、检索、预览、抽取、比较、下载。

安全约束:
- 档案文件只读,注册后 chmod 0444,任何接口都不修改档案。
- 预览接口带严格 CSP(default-src 'none'),前端 iframe 全沙箱,
  不会访问档案中引用的任何外部网址。
- 仅监听本地地址(默认 127.0.0.1)。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import engine, fixtures
from .indexer import Indexer, normalize_date, scan_archive, utcnow
from .warc import sniff_format

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_JSON_BODY = 1 << 20


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def record_public(row):
    """索引行 → 前端 JSON(解析头部 JSON 字段)。"""
    if row is None:
        return None
    out = dict(row)
    for k in ("warc_headers", "http_headers"):
        if out.get(k):
            try:
                out[k] = json.loads(out[k])
            except (ValueError, TypeError):
                pass
    return out


def result_public(row):
    out = dict(row)
    if out.get("fields_json"):
        try:
            out["fields"] = json.loads(out["fields_json"])
        except (ValueError, TypeError):
            out["fields"] = None
    return out


class App:
    def __init__(self, data_dir):
        self.data_dir = os.path.abspath(data_dir)
        self.archives_dir = os.path.join(self.data_dir, "archives")
        os.makedirs(self.archives_dir, exist_ok=True)
        self.indexer = Indexer(os.path.join(self.data_dir, "index.sqlite3"))
        # 上次进程退出时可能遗留 scanning 状态,线程已不存在,归为 paused(可继续)
        with self.indexer._lock:
            self.indexer._conn.execute(
                "UPDATE archives SET scan_status='paused' WHERE scan_status='scanning'")
            self.indexer._conn.commit()
        self._scan_lock = threading.Lock()
        self._scans = {}  # archive_id -> {"stop": Event, "thread": Thread}

    # ------------------------------------------------------------ 档案注册

    def register_archive(self, src_path, name):
        """把文件登记为档案:计算哈希、嗅探格式、移入独立目录、设为只读。"""
        fmt = sniff_format(src_path)
        h = hashlib.sha256()
        size = 0
        with open(src_path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                size += len(chunk)
        archive_id = self.indexer.create_archive(name, "", fmt, size, h.hexdigest())
        dest_dir = os.path.join(self.archives_dir, str(archive_id))
        os.makedirs(dest_dir)
        dest = os.path.join(dest_dir, name)
        shutil.move(src_path, dest)
        os.chmod(dest, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # 0444 只读
        self.indexer.update_archive(archive_id, path=dest)
        return self.indexer.get_archive(archive_id)

    # ------------------------------------------------------------ 扫描控制

    def start_scan(self, archive_id, force=False):
        arch = self.indexer.get_archive(archive_id)
        if arch is None:
            raise ApiError(404, "archive not found")
        with self._scan_lock:
            cur = self._scans.get(archive_id)
            if cur and cur["thread"].is_alive():
                raise ApiError(409, "scan already running")
            stop = threading.Event()
            t = threading.Thread(target=self._scan_worker,
                                 args=(archive_id, stop, force), daemon=True)
            self._scans[archive_id] = {"stop": stop, "thread": t}
            t.start()
        return {"status": "started"}

    def _scan_worker(self, archive_id, stop, force):
        try:
            scan_archive(self.indexer, archive_id, stop_event=stop, force=force)
        except Exception as exc:  # scan_archive 已记录状态
            print(f"scan worker error for archive {archive_id}: {exc}")

    def stop_scan(self, archive_id):
        with self._scan_lock:
            cur = self._scans.get(archive_id)
            if cur and cur["thread"].is_alive():
                cur["stop"].set()
                return {"status": "stopping"}
        return {"status": "not-running"}

    def scan_running(self, archive_id):
        cur = self._scans.get(archive_id)
        return bool(cur and cur["thread"].is_alive())

    # ------------------------------------------------------------ 请求处理

    def handle(self, req: BaseHTTPRequestHandler, method: str):
        parsed = urllib.parse.urlparse(req.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            for m, pattern, func in ROUTES:
                if m != method:
                    continue
                match = re.fullmatch(pattern, path)
                if match:
                    func(self, req, query, **match.groupdict())
                    return
            raise ApiError(404, f"no route: {method} {path}")
        except ApiError as exc:
            self._send_json(req, exc.status, {"error": exc.message})
        except json.JSONDecodeError as exc:
            self._send_json(req, 400, {"error": f"bad json: {exc}"})
        except Exception as exc:
            self._send_json(req, 500, {"error": f"{type(exc).__name__}: {exc}"})

    # ------------------------------------------------------------ 响应工具

    @staticmethod
    def _send_json(req, status, obj, extra_headers=None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        req.send_response(status)
        req.send_header("Content-Type", "application/json; charset=utf-8")
        req.send_header("Content-Length", str(len(data)))
        req.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            req.send_header(k, v)
        req.end_headers()
        req.wfile.write(data)

    @staticmethod
    def _send_bytes(req, status, data, content_type, extra_headers=None):
        req.send_response(status)
        req.send_header("Content-Type", content_type)
        req.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or {}).items():
            req.send_header(k, v)
        req.end_headers()
        req.wfile.write(data)

    def _read_json(self, req):
        length = int(req.headers.get("Content-Length") or 0)
        if length > MAX_JSON_BODY:
            raise ApiError(413, "json body too large")
        raw = req.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _get_archive(self, archive_id):
        arch = self.indexer.get_archive(int(archive_id))
        if arch is None:
            raise ApiError(404, "archive not found")
        return arch

    def _get_record(self, pk):
        row = self.indexer.get_record(int(pk))
        if row is None:
            raise ApiError(404, "record not found")
        return row

    # ------------------------------------------------------------ 路由实现

    def r_index(self, req, query):
        with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
            data = f.read()
        self._send_bytes(req, 200, data, "text/html; charset=utf-8",
                         {"Content-Security-Policy": "default-src 'self'",
                          "Cache-Control": "no-store"})

    def r_static(self, req, query, name):
        if not re.fullmatch(r"[\w.\-]+", name) or ".." in name:
            raise ApiError(400, "bad name")
        path = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(path):
            raise ApiError(404, "not found")
        ctype = {".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".html": "text/html; charset=utf-8"}.get(
                     os.path.splitext(name)[1], "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        self._send_bytes(req, 200, data, ctype, {"Cache-Control": "no-store"})

    def r_upload(self, req, query):
        name = (query.get("name") or ["archive.warc.gz"])[0]
        name = os.path.basename(name).replace("..", "_") or "archive.warc.gz"
        length = req.headers.get("Content-Length")
        if length is None:
            raise ApiError(411, "Content-Length required")
        length = int(length)
        tmp = os.path.join(self.archives_dir, f".upload-{os.getpid()}-{threading.get_ident()}")
        try:
            remaining = length
            with open(tmp, "wb") as f:
                while remaining > 0:
                    chunk = req.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        raise ApiError(400, "unexpected end of upload")
                    f.write(chunk)
                    remaining -= len(chunk)
            arch = self.register_archive(tmp, name)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self._send_json(req, 201, arch)

    def r_make_fixture(self, req, query):
        tmp = os.path.join(self.archives_dir, f".fixture-{os.getpid()}-{threading.get_ident()}")
        try:
            fixtures.write_fixture(tmp)
            arch = self.register_archive(tmp, "sample.warc.gz")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self._send_json(req, 201, arch)

    def r_list_archives(self, req, query):
        rows = self.indexer.list_archives()
        for r in rows:
            r["scanning"] = self.scan_running(r["id"])
        self._send_json(req, 200, {"archives": rows})

    def r_get_archive(self, req, query, id):
        arch = self._get_archive(id)
        counts = [a for a in self.indexer.list_archives() if a["id"] == arch["id"]]
        out = counts[0] if counts else arch
        out["scanning"] = self.scan_running(arch["id"])
        self._send_json(req, 200, out)

    def r_start_scan(self, req, query, id):
        self._get_archive(id)
        body = self._read_json(req)
        self._send_json(req, 200, self.start_scan(int(id), force=bool(body.get("force"))))

    def r_stop_scan(self, req, query, id):
        self._send_json(req, 200, self.stop_scan(int(id)))

    def r_records(self, req, query, id):
        self._get_archive(id)
        rows, total = self.indexer.query_records(
            int(id),
            url=(query.get("url") or [None])[0],
            types=[t for t in (query.get("type") or [""])[0].split(",") if t] or None,
            status=(query.get("status") or [None])[0],
            time_from=(query.get("from") or [None])[0],
            time_to=(query.get("to") or [None])[0],
            limit=int((query.get("limit") or ["200"])[0]),
            offset=int((query.get("offset") or ["0"])[0]))
        self._send_json(req, 200, {"total": total,
                                   "records": [record_public(r) for r in rows]})

    def r_urls(self, req, query, id):
        self._get_archive(id)
        rows = self.indexer.list_urls(int(id), q=(query.get("q") or [None])[0])
        self._send_json(req, 200, {"urls": rows})

    def r_record_detail(self, req, query, id):
        self._send_json(req, 200, record_public(self._get_record(id)))

    def _payload_for(self, pk):
        row = self._get_record(pk)
        arch = self._get_archive(row["archive_id"])
        info = engine.decode_record_text(self.indexer, arch, row)
        return row, arch, info

    def r_record_content(self, req, query, id):
        row, _arch, info = self._payload_for(id)
        if (query.get("raw") or [None])[0] == "1":
            if not info["ok"]:
                raise ApiError(422, info["error"])
            self._send_bytes(req, 200, info["payload"], "application/octet-stream",
                             {"Content-Disposition": f'attachment; filename="record-{id}.bin"'})
            return
        if not info["ok"]:
            self._send_json(req, 200, {"ok": False, "error": info["error"],
                                       "record": record_public(row)})
            return
        self._send_json(req, 200, {
            "ok": True, "record": record_public(row),
            "resolved_from": info.get("resolved_from"),
            "http_status": info.get("http_status"),
            "content_type": info.get("content_type"),
            "charset": info.get("charset"),
            "charset_error": info.get("charset_error"),
            "content_encoding_error": info.get("ce_error"),
            "text": info.get("text"),
        })

    def r_record_preview(self, req, query, id):
        row, _arch, info = self._payload_for(id)
        # 严格 CSP:禁止一切外部加载,确保不访问档案中的外部网址
        csp = ("default-src 'none'; style-src 'unsafe-inline'; "
               "img-src data:; font-src data:")
        headers = {"Content-Security-Policy": csp,
                   "X-Content-Type-Options": "nosniff",
                   "Cache-Control": "no-store"}
        if not info["ok"]:
            body = (f"<h3>无法预览</h3><pre>{info['error']}</pre>").encode("utf-8")
            self._send_bytes(req, 200, body, "text/html; charset=utf-8", headers)
            return
        ctype = (info.get("content_type") or "").lower()
        if "html" in ctype:
            data = info["text"].encode("utf-8")
            if info.get("resolved_from"):
                headers["X-Resolved-From"] = info["resolved_from"]
            self._send_bytes(req, 200, data, "text/html; charset=utf-8", headers)
        else:
            self._send_bytes(req, 200, info["text"].encode("utf-8"),
                             "text/plain; charset=utf-8", headers)

    def r_create_run(self, req, query):
        body = self._read_json(req)
        arch = self._get_archive(body.get("archive_id"))
        url = body.get("url")
        if not url:
            raise ApiError(400, "url required")
        rules = body.get("rules") or {"title": True, "tables": True}
        run_id = self.indexer.create_run(
            arch["id"], url,
            normalize_date(body.get("time_from") or "") or body.get("time_from"),
            normalize_date(body.get("time_to") or "") or body.get("time_to"),
            rules)
        run = engine.execute_run(self.indexer, run_id)
        results = [result_public(r) for r in self.indexer.run_results(run_id)]
        self._send_json(req, 201, {"run": run, "results": results})

    def r_list_runs(self, req, query):
        aid = (query.get("archive_id") or [None])[0]
        self._send_json(req, 200, {"runs": self.indexer.list_runs(
            int(aid) if aid else None)})

    def r_get_run(self, req, query, id):
        run = self.indexer.get_run(int(id))
        if run is None:
            raise ApiError(404, "run not found")
        results = [result_public(r) for r in self.indexer.run_results(run["id"])]
        self._send_json(req, 200, {"run": run, "results": results})

    def r_rerun(self, req, query, id):
        run = self.indexer.get_run(int(id))
        if run is None:
            raise ApiError(404, "run not found")
        run = engine.execute_run(self.indexer, run["id"])
        results = [result_public(r) for r in self.indexer.run_results(run["id"])]
        self._send_json(req, 200, {"run": run, "results": results})

    def r_download_run(self, req, query, id):
        run = self.indexer.get_run(int(id))
        if run is None:
            raise ApiError(404, "run not found")
        results = [result_public(r) for r in self.indexer.run_results(run["id"])]
        fmt = (query.get("format") or ["csv"])[0]
        if fmt == "json":
            data = json.dumps({"run": run, "results": results},
                              ensure_ascii=False, indent=2).encode("utf-8")
            self._send_bytes(req, 200, data, "application/json; charset=utf-8",
                             {"Content-Disposition": f'attachment; filename="run-{id}.json"'})
            return
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["timestamp", "capture_id", "digest", "file_offset",
                    "title", "tables", "fields", "error"])
        for r in results:
            fields = r.get("fields") or {}
            w.writerow([r.get("timestamp"), r.get("capture_id"), r.get("digest"),
                        r.get("file_offset"), fields.get("title"),
                        json.dumps(fields.get("tables"), ensure_ascii=False),
                        json.dumps(fields.get("fields"), ensure_ascii=False),
                        r.get("error")])
        data = b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")  # BOM 便于 Excel 打开
        self._send_bytes(req, 200, data, "text/csv; charset=utf-8",
                         {"Content-Disposition": f'attachment; filename="run-{id}.csv"'})

    def r_compare(self, req, query):
        body = self._read_json(req)
        ids = body.get("record_ids") or []
        if len(ids) != 2:
            raise ApiError(400, "record_ids must contain exactly 2 ids")
        row_a = self._get_record(ids[0])
        arch = self._get_archive(row_a["archive_id"])
        rules = body.get("rules") or {"title": True, "tables": True}
        out = engine.compare_records(self.indexer, arch, ids[0], ids[1], rules)
        for key in ("a", "b"):
            if key in out and "record" in out[key]:
                out[key]["record"] = record_public(out[key]["record"])
        self._send_json(req, 200, out)


ROUTES = [
    ("GET", r"/", App.r_index),
    ("GET", r"/static/(?P<name>[\w.\-]+)", App.r_static),
    ("POST", r"/api/archives", App.r_upload),
    ("GET", r"/api/archives", App.r_list_archives),
    ("POST", r"/api/fixtures", App.r_make_fixture),
    ("GET", r"/api/archives/(?P<id>\d+)", App.r_get_archive),
    ("POST", r"/api/archives/(?P<id>\d+)/scan", App.r_start_scan),
    ("POST", r"/api/archives/(?P<id>\d+)/stop", App.r_stop_scan),
    ("GET", r"/api/archives/(?P<id>\d+)/records", App.r_records),
    ("GET", r"/api/archives/(?P<id>\d+)/urls", App.r_urls),
    ("GET", r"/api/records/(?P<id>\d+)", App.r_record_detail),
    ("GET", r"/api/records/(?P<id>\d+)/content", App.r_record_content),
    ("GET", r"/api/records/(?P<id>\d+)/preview", App.r_record_preview),
    ("POST", r"/api/runs", App.r_create_run),
    ("GET", r"/api/runs", App.r_list_runs),
    ("GET", r"/api/runs/(?P<id>\d+)", App.r_get_run),
    ("POST", r"/api/runs/(?P<id>\d+)/rerun", App.r_rerun),
    ("GET", r"/api/runs/(?P<id>\d+)/download", App.r_download_run),
    ("POST", "/api/compare", App.r_compare),
]


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            app.handle(self, "GET")

        def do_POST(self):
            app.handle(self, "POST")

        def log_message(self, fmt, *args):
            pass

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="warcview 本地 WARC 检索服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default="./data", help="数据目录(索引与档案)")
    args = parser.parse_args(argv)
    app = App(args.data)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"warcview 已启动: http://{args.host}:{args.port}  数据目录: {app.data_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
