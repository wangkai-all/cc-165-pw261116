"""运行时引擎:按偏移读取正文、revisit 引用解析、抽取任务执行、版本比较。"""
from __future__ import annotations

import json

from . import warc
from .extract import (DecodeError, decode_content_encoding, decode_text,
                      diff_fields, extract_fields, parse_http_message)
from .indexer import Indexer, utcnow

MAX_REVISIT_HOPS = 5


def load_record_payload(indexer: Indexer, archive: dict, rec: dict) -> dict:
    """按索引行从档案中读取正文(只读)。

    revisit 记录沿 WARC-Refers-To 解析到原始响应;引用缺失时返回错误,
    不猜测、不伪造内容。返回 dict:
      ok, payload, content_type, http_status, http_headers,
      ce_error, resolved_from, source(实际取内容的记录行), error
    """
    row = rec
    resolved_from = None
    hops = 0
    while row["type"] == "revisit":
        hops += 1
        if hops > MAX_REVISIT_HOPS:
            return {"ok": False, "error": "revisit reference chain too long"}
        target_id = row.get("refers_to")
        if not target_id:
            return {"ok": False, "error": "revisit record has no WARC-Refers-To"}
        target = indexer.find_by_record_id(row["archive_id"], target_id)
        if target is None:
            return {"ok": False,
                    "error": f"missing reference: record {target_id} not found in archive"}
        resolved_from = target["record_id"]
        row = target
    try:
        with open(archive["path"], "rb") as f:
            record = warc.load_record_at(f, archive["format"], row["file_offset"])
    except (warc.WarcError, EOFError, OSError) as exc:
        return {"ok": False,
                "error": f"re-read failed at offset {row['file_offset']}: {exc}"}
    payload = record.content
    content_type = record.headers.get("content-type", "") or ""
    http_headers = {}
    http_status = None
    if content_type.startswith("application/http") and record.content:
        try:
            status_line, http_headers, payload = parse_http_message(record.content)
            content_type = http_headers.get("content-type", content_type)
            parts = status_line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                http_status = int(parts[1])
        except DecodeError as exc:
            return {"ok": False, "error": f"http parse failed: {exc}",
                    "resolved_from": resolved_from}
    decoded, ce_error = decode_content_encoding(payload, http_headers)
    return {"ok": True, "payload": decoded, "content_type": content_type,
            "http_status": http_status, "http_headers": http_headers,
            "ce_error": ce_error, "resolved_from": resolved_from, "source": row}


def decode_record_text(indexer: Indexer, archive: dict, rec: dict) -> dict:
    """load_record_payload + 字符集解码,返回带 text/charset 的完整信息。"""
    info = load_record_payload(indexer, archive, rec)
    if not info["ok"]:
        return info
    text, charset, cs_err = decode_text(info["payload"], info.get("content_type") or "")
    info.update({"text": text, "charset": charset, "charset_error": cs_err})
    return info


def execute_run(indexer: Indexer, run_id):
    """执行抽取任务:对时间范围内的每条捕获应用规则,结果逐条入库。

    每条结果携带捕获编号、正文指纹、来源偏移;解码失败的捕获保留错误信息,
    不会被跳过。
    """
    run = indexer.get_run(run_id)
    if run is None:
        raise KeyError(f"run {run_id} not found")
    archive = indexer.get_archive(run["archive_id"])
    rules = json.loads(run["rules_json"])
    rows, _ = indexer.query_records(
        run["archive_id"], url_exact=run["url"], types=("response", "revisit"),
        time_from=run["time_from"], time_to=run["time_to"], limit=1_000_000)
    indexer.delete_results(run_id)
    count = 0
    for row in rows:
        error = None
        fields = {}
        info = decode_record_text(indexer, archive, row)
        if not info["ok"]:
            error = info["error"]
        else:
            errs = [e for e in (info.get("ce_error"), info.get("charset_error")) if e]
            error = "; ".join(errs) or None
            try:
                fields = extract_fields(info["text"], rules)
            except Exception as exc:
                error = (error + "; " if error else "") + f"extract failed: {exc}"
        indexer.insert_result({
            "run_id": run_id, "record_ref": row["id"],
            "capture_id": row["record_id"], "url": row["url"],
            "timestamp": row["timestamp"], "digest": row["digest"],
            "file_offset": row["file_offset"],
            "fields_json": json.dumps(fields, ensure_ascii=False),
            "error": error})
        count += 1
    indexer.update_run(run_id, status="done", executed_at=utcnow(),
                       result_count=count, executions=run["executions"] + 1)
    return indexer.get_run(run_id)


def compare_records(indexer: Indexer, archive: dict, pk_a, pk_b, rules) -> dict:
    """比较两个捕获版本的抽取字段。"""
    out = {}
    texts = {}
    for key, pk in (("a", pk_a), ("b", pk_b)):
        row = indexer.get_record(pk)
        if row is None:
            raise KeyError(f"record {pk} not found")
        info = decode_record_text(indexer, archive, row)
        if not info["ok"]:
            out[key] = {"error": info["error"], "record": row}
            texts[key] = None
            continue
        warnings = [e for e in (info.get("ce_error"), info.get("charset_error")) if e]
        fields = extract_fields(info["text"], rules)
        out[key] = {"fields": fields, "record": row, "charset": info["charset"],
                    "warnings": warnings}
        texts[key] = info["text"]
    if texts.get("a") is not None and texts.get("b") is not None:
        out["diff"] = diff_fields(out["a"]["fields"], out["b"]["fields"])
    return out
