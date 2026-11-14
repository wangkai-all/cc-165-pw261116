"""SQLite 索引层与可恢复扫描器。

- 每条记录一行,保留物理偏移(file_offset)、捕获编号(record_id)、正文指纹(digest)。
- 损坏记录同样入库(type='error', status='corrupt'),带偏移与错误信息,绝不丢弃。
- 扫描进度(archives.scan_offset)逐条提交,中断后从断点继续。
- 相同正文的多次捕获各自成行,保留各自时间戳。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

from . import warc
from .extract import DecodeError, decode_content_encoding, parse_http_message

SCHEMA = """
CREATE TABLE IF NOT EXISTS archives (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  path TEXT NOT NULL,
  format TEXT NOT NULL,
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  uploaded_at TEXT NOT NULL,
  scan_status TEXT NOT NULL DEFAULT 'pending',
  scan_offset INTEGER NOT NULL DEFAULT 0,
  scan_error TEXT,
  scanned_at TEXT
);
CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  archive_id INTEGER NOT NULL REFERENCES archives(id),
  file_offset INTEGER NOT NULL,
  member_length INTEGER NOT NULL DEFAULT 0,
  record_id TEXT,
  type TEXT NOT NULL,
  url TEXT,
  timestamp TEXT,
  status TEXT NOT NULL DEFAULT 'ok',
  error TEXT,
  digest TEXT,
  declared_digest TEXT,
  refers_to TEXT,
  refers_to_date TEXT,
  http_status INTEGER,
  content_type TEXT,
  payload_length INTEGER,
  warc_headers TEXT,
  http_headers TEXT,
  UNIQUE (archive_id, file_offset)
);
CREATE INDEX IF NOT EXISTS idx_records_url ON records(archive_id, url, timestamp);
CREATE INDEX IF NOT EXISTS idx_records_digest ON records(archive_id, digest);
CREATE INDEX IF NOT EXISTS idx_records_rid ON records(archive_id, record_id);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  archive_id INTEGER NOT NULL REFERENCES archives(id),
  url TEXT NOT NULL,
  time_from TEXT,
  time_to TEXT,
  rules_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'done',
  created_at TEXT NOT NULL,
  executed_at TEXT,
  executions INTEGER NOT NULL DEFAULT 0,
  result_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  record_ref INTEGER,
  capture_id TEXT,
  url TEXT,
  timestamp TEXT,
  digest TEXT,
  file_offset INTEGER,
  fields_json TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);
"""

RECORD_COLS = ["archive_id", "file_offset", "member_length", "record_id", "type",
               "url", "timestamp", "status", "error", "digest", "declared_digest",
               "refers_to", "refers_to_date", "http_status", "content_type",
               "payload_length", "warc_headers", "http_headers"]


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_date(value):
    """WARC-Date 归一化为 UTC 'YYYY-MM-DDTHH:MM:SSZ';无法解析时保留原样。"""
    if not value:
        return None
    v = value.strip()
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return v


def _rid(value):
    """归一化记录 ID:去掉 <> 与 urn:uuid: 前缀,便于跨记录匹配。"""
    v = (value or "").strip().strip("<>").strip()
    if v.lower().startswith("urn:uuid:"):
        v = v[9:]
    return v or None


class Indexer:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ archives

    def create_archive(self, name, path, fmt, size, sha256):
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO archives(name, path, format, size, sha256, uploaded_at)"
                " VALUES (?,?,?,?,?,?)",
                (name, path, fmt, size, sha256, utcnow()))
            self._conn.commit()
            return cur.lastrowid

    def update_archive(self, archive_id, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE archives SET {cols} WHERE id=?",
                               (*fields.values(), archive_id))
            self._conn.commit()

    def get_archive(self, archive_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
        return dict(row) if row else None

    def list_archives(self):
        with self._lock:
            rows = self._conn.execute("""
              SELECT a.*,
                (SELECT COUNT(*) FROM records r WHERE r.archive_id=a.id
                   AND r.type != 'error') AS record_count,
                (SELECT COUNT(*) FROM records r WHERE r.archive_id=a.id
                   AND r.type = 'error') AS error_count
              FROM archives a ORDER BY a.id""").fetchall()
        return [dict(r) for r in rows]

    def reset_archive(self, archive_id):
        with self._lock:
            self._conn.execute("DELETE FROM records WHERE archive_id=?", (archive_id,))
            self._conn.execute(
                "UPDATE archives SET scan_offset=0, scan_status='pending',"
                " scan_error=NULL, scanned_at=NULL WHERE id=?", (archive_id,))
            self._conn.commit()

    # ------------------------------------------------------------ records

    def insert_record(self, row):
        vals = [row.get(c) for c in RECORD_COLS]
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO records({','.join(RECORD_COLS)})"
                f" VALUES ({','.join('?' * len(RECORD_COLS))})", vals)
            self._conn.commit()

    def get_record(self, record_pk):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM records WHERE id=?", (record_pk,)).fetchone()
        return dict(row) if row else None

    def find_by_record_id(self, archive_id, record_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM records WHERE archive_id=? AND record_id=?"
                " ORDER BY timestamp LIMIT 1",
                (archive_id, _rid(record_id))).fetchone()
        return dict(row) if row else None

    def query_records(self, archive_id, url=None, url_exact=None, types=None,
                      status=None, time_from=None, time_to=None,
                      limit=200, offset=0, order="timestamp"):
        sql = "SELECT * FROM records WHERE archive_id=?"
        count_sql = "SELECT COUNT(*) FROM records WHERE archive_id=?"
        args = [archive_id]
        where = ""
        if url:
            where += " AND url LIKE ?"
            args.append(f"%{url}%")
        if url_exact:
            where += " AND url = ?"
            args.append(url_exact)
        if types:
            where += f" AND type IN ({','.join('?' * len(types))})"
            args.extend(types)
        if status:
            where += " AND status = ?"
            args.append(status)
        if time_from:
            where += " AND timestamp >= ?"
            args.append(normalize_date(time_from) or time_from)
        if time_to:
            where += " AND timestamp <= ?"
            args.append(normalize_date(time_to) or time_to)
        with self._lock:
            total = self._conn.execute(count_sql + where, args).fetchone()[0]
            direction = "DESC" if order.endswith(" desc") else "ASC"
            rows = self._conn.execute(
                sql + where + f" ORDER BY timestamp {direction}, file_offset {direction}"
                " LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return [dict(r) for r in rows], total

    def list_urls(self, archive_id, q=None):
        sql = """
          SELECT url, COUNT(*) AS captures, MIN(timestamp) AS first, MAX(timestamp) AS last,
            SUM(CASE WHEN type='response' THEN 1 ELSE 0 END) AS responses,
            SUM(CASE WHEN type='revisit' THEN 1 ELSE 0 END) AS revisits,
            COUNT(DISTINCT digest) AS bodies
          FROM records
          WHERE archive_id=? AND url IS NOT NULL AND url != '' AND type != 'error'"""
        args = [archive_id]
        if q:
            sql += " AND url LIKE ?"
            args.append(f"%{q}%")
        sql += " GROUP BY url ORDER BY url"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ runs

    def create_run(self, archive_id, url, time_from, time_to, rules):
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs(archive_id, url, time_from, time_to, rules_json,"
                " status, created_at) VALUES (?,?,?,?,?,'pending',?)",
                (archive_id, url, time_from, time_to,
                 json.dumps(rules, ensure_ascii=False), utcnow()))
            self._conn.commit()
            return cur.lastrowid

    def get_run(self, run_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, archive_id=None):
        with self._lock:
            if archive_id:
                rows = self._conn.execute(
                    "SELECT * FROM runs WHERE archive_id=? ORDER BY id DESC",
                    (archive_id,)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def update_run(self, run_id, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE runs SET {cols} WHERE id=?",
                               (*fields.values(), run_id))
            self._conn.commit()

    def delete_results(self, run_id):
        with self._lock:
            self._conn.execute("DELETE FROM results WHERE run_id=?", (run_id,))
            self._conn.commit()

    def insert_result(self, row):
        cols = ["run_id", "record_ref", "capture_id", "url", "timestamp",
                "digest", "file_offset", "fields_json", "error"]
        with self._lock:
            self._conn.execute(
                f"INSERT INTO results({','.join(cols)})"
                f" VALUES ({','.join('?' * len(cols))})",
                [row.get(c) for c in cols])
            self._conn.commit()

    def run_results(self, run_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM results WHERE run_id=? ORDER BY timestamp, id",
                (run_id,)).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- 记录分析

def analyze_record(archive_id, rec: warc.WarcRecord) -> dict:
    """把解析后的 WARC 记录转换为索引行。HTTP 解析/内容编码失败仅标记,不丢弃。"""
    h = rec.headers
    rtype = (h.get("warc-type") or "unknown").lower()
    row = {
        "archive_id": archive_id,
        "file_offset": rec.offset,
        "member_length": rec.member_length,
        "record_id": _rid(h.get("warc-record-id")),
        "type": rtype,
        "url": h.get("warc-target-uri"),
        "timestamp": normalize_date(h.get("warc-date", "")),
        "status": "ok",
        "error": None,
        "digest": None,
        "declared_digest": h.get("warc-payload-digest"),
        "refers_to": _rid(h.get("warc-refers-to")),
        "refers_to_date": h.get("warc-refers-to-date"),
        "http_status": None,
        "content_type": h.get("content-type"),
        "payload_length": None,
        "warc_headers": json.dumps(h, ensure_ascii=False),
        "http_headers": None,
    }
    payload = rec.content
    ctype = h.get("content-type", "") or ""
    if ctype.startswith("application/http") and rec.content:
        try:
            status_line, http_headers, payload = parse_http_message(rec.content)
            row["http_headers"] = json.dumps(http_headers, ensure_ascii=False)
            row["content_type"] = http_headers.get("content-type", ctype)
            parts = status_line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                row["http_status"] = int(parts[1])
        except DecodeError as exc:
            row["status"] = "decode_error"
            row["error"] = f"http parse failed: {exc}"
            payload = rec.content
    row["payload_length"] = len(payload)
    if rtype in ("response", "resource"):
        if row["http_headers"]:
            _, ce_err = decode_content_encoding(payload, json.loads(row["http_headers"]))
            if ce_err:
                row["status"] = "decode_error"
                row["error"] = ce_err
        row["digest"] = "sha256:" + hashlib.sha256(payload).hexdigest()
    elif rtype == "revisit":
        # revisit 无正文,指纹取声明的引用正文摘要,便于与原始捕获归组
        row["digest"] = row["declared_digest"]
    return row


# ---------------------------------------------------------------- 扫描

def _resync(f, start, size, fmt, max_attempts=500):
    """从 start 起寻找下一条可解析记录,返回偏移或 None。"""
    candidate = warc.find_next_member(f, start, size, fmt)
    attempts = 0
    while candidate is not None and attempts < max_attempts:
        try:
            warc.load_record_at(f, fmt, candidate)
            return candidate
        except (warc.WarcError, EOFError):
            attempts += 1
            candidate = warc.find_next_member(f, candidate + 1, size, fmt)
    return None


def scan_archive(indexer: Indexer, archive_id, max_records=None, stop_event=None,
                 force=False, progress=None):
    """扫描档案建立索引。可从上次 scan_offset 继续;force=True 重新扫描。

    max_records 用于测试中断恢复;stop_event 用于运行中停止。
    """
    arch = indexer.get_archive(archive_id)
    if arch is None:
        raise KeyError(f"archive {archive_id} not found")
    if force:
        indexer.reset_archive(archive_id)
        arch = indexer.get_archive(archive_id)
    fmt = arch["format"]
    path = arch["path"]
    size = os.path.getsize(path)
    offset = arch["scan_offset"]
    if offset >= size and arch["scan_status"] == "done":
        return indexer.get_archive(archive_id)
    indexer.update_archive(archive_id, scan_status="scanning", scan_error=None)
    processed = 0
    status = "done"
    try:
        with open(path, "rb") as f:
            while True:
                if stop_event is not None and stop_event.is_set():
                    status = "paused"
                    break
                if max_records is not None and processed >= max_records:
                    status = "paused"
                    break
                if offset >= size:
                    status = "done"
                    break
                try:
                    rec = warc.load_record_at(f, fmt, offset)
                    indexer.insert_record(analyze_record(archive_id, rec))
                    offset = rec.end_offset
                except EOFError:
                    status = "done"
                    break
                except warc.WarcError as exc:
                    # 损坏记录:保留偏移与错误,重同步继续
                    indexer.insert_record({
                        "archive_id": archive_id, "file_offset": offset,
                        "member_length": 0, "record_id": None, "type": "error",
                        "url": None, "timestamp": None, "status": "corrupt",
                        "error": exc.message})
                    nxt = _resync(f, offset + 1, size, fmt)
                    if nxt is None:
                        offset = size
                        processed += 1
                        indexer.update_archive(archive_id, scan_offset=offset)
                        status = "done"
                        break
                    offset = nxt
                processed += 1
                indexer.update_archive(archive_id, scan_offset=offset)
                if progress:
                    progress(offset, size)
    except Exception as exc:  # 扫描器自身故障:保留断点,标记错误
        indexer.update_archive(archive_id, scan_status="error",
                               scan_error=str(exc), scan_offset=offset)
        raise
    indexer.update_archive(archive_id, scan_status=status, scan_offset=offset,
                           scanned_at=utcnow() if status == "done" else None)
    return indexer.get_archive(archive_id)
