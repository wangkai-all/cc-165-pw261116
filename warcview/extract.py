"""正文解码(HTTP / 传输编码 / 内容编码 / 字符集)与字段抽取、差异比较。

原则:任何一步失败都返回错误信息并保留原始字节,绝不静默丢弃。
"""
from __future__ import annotations

import re
import zlib
from html.parser import HTMLParser


class DecodeError(Exception):
    pass


# ---------------------------------------------------------------- HTTP 解析

def parse_http_message(content: bytes):
    """解析 application/http 内容块 → (status_line, headers, body)。

    处理 Transfer-Encoding: chunked。失败抛 DecodeError。
    """
    end = content.find(b"\r\n\r\n")
    sep = 4
    if end < 0:
        end = content.find(b"\n\n")
        sep = 2
    if end < 0:
        raise DecodeError("HTTP header terminator not found")
    head = content[:end].decode("iso-8859-1")
    lines = head.splitlines()
    if not lines:
        raise DecodeError("empty HTTP head")
    status_line = lines[0].strip()
    headers = {}
    last = None
    for raw in lines[1:]:
        if not raw:
            continue
        if raw[0] in " \t" and last:
            headers[last] += " " + raw.strip()
            continue
        if ":" not in raw:
            raise DecodeError(f"malformed HTTP header line: {raw!r}")
        k, v = raw.split(":", 1)
        last = k.strip().lower()
        headers[last] = v.strip()
    body = content[end + sep:]
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = _dechunk(body)
    return status_line, headers, body


def _dechunk(body: bytes) -> bytes:
    out = bytearray()
    i = 0
    while True:
        j = body.find(b"\r\n", i)
        if j < 0:
            raise DecodeError("bad chunked body: missing chunk size")
        size_token = body[i:j].split(b";", 1)[0].strip()
        try:
            size = int(size_token, 16)
        except ValueError:
            raise DecodeError(f"bad chunk size: {size_token!r}")
        i = j + 2
        if size == 0:
            return bytes(out)
        if i + size > len(body):
            raise DecodeError("truncated chunk data")
        out += body[i:i + size]
        i += size
        if body[i:i + 2] != b"\r\n":
            raise DecodeError("missing chunk terminator")
        i += 2


def decode_content_encoding(body: bytes, headers: dict):
    """解除 Content-Encoding,返回 (字节, 错误信息或 None)。

    失败时返回原始字节并给出错误,调用方必须保留该错误。
    """
    ce = headers.get("content-encoding", "").lower().strip()
    if not ce or ce == "identity":
        return body, None
    try:
        if ce in ("gzip", "x-gzip"):
            d = zlib.decompressobj(31)
            return d.decompress(body) + d.flush(), None
        if ce == "deflate":
            try:
                return zlib.decompress(body), None
            except zlib.error:
                return zlib.decompress(body, -15), None
        return body, f"unsupported content-encoding: {ce}"
    except zlib.error as exc:
        return body, f"content-encoding decode failed: {exc}"


# ---------------------------------------------------------------- 字符集

_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?\s*([\w.\-]+)", re.I)
_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([\w.\-]+)", re.I)


def decode_text(payload: bytes, content_type_header: str = ""):
    """按声明字符集解码;失败时回退并明确标记错误,绝不丢弃数据。

    返回 (text, charset_used, error_or_None)。
    """
    declared = None
    m = _CHARSET_RE.search(content_type_header or "")
    if m:
        declared = m.group(1)
    if not declared:
        m = _META_CHARSET_RE.search(payload[:4096])
        if m:
            declared = m.group(1).decode("ascii", "replace")
    candidates = []
    for cs in ([declared] if declared else []) + ["utf-8", "iso-8859-1"]:
        if cs and cs.lower() not in [c.lower() for c in candidates]:
            candidates.append(cs)
    first_error = None
    for idx, cs in enumerate(candidates):
        try:
            text = payload.decode(cs)
        except (LookupError, UnicodeDecodeError) as exc:
            if first_error is None:
                first_error = f"charset {cs!r} failed: {exc}"
            continue
        err = None
        if idx > 0 and first_error:
            err = f"{first_error}; decoded with fallback {cs!r}"
        return text, cs, err
    # iso-8859-1 不会失败,这里只是兜底
    return payload.decode("latin-1", "replace"), "latin-1", first_error


# ---------------------------------------------------------------- 字段抽取

class _HTMLExtract(HTMLParser):
    """提取 <title> 文本与全部 <table> 结构。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.title_parts = []
        self.tables = []           # [{"rows": [[cell, ...], ...]}]
        self._stack = []           # 打开的 table(支持嵌套)
        self._skip = 0             # script/style 深度

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        elif tag in ("script", "style"):
            self._skip += 1
        elif self._skip:
            return
        elif tag == "table":
            self._stack.append({"rows": [], "cur_row": None, "cur_cell": None})
        elif tag == "tr" and self._stack:
            self._stack[-1]["cur_row"] = []
        elif tag in ("td", "th") and self._stack and self._stack[-1]["cur_row"] is not None:
            self._stack[-1]["cur_cell"] = []

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif self._skip:
            return
        elif tag in ("td", "th") and self._stack:
            t = self._stack[-1]
            if t["cur_cell"] is not None and t["cur_row"] is not None:
                t["cur_row"].append(" ".join("".join(t["cur_cell"]).split()))
            t["cur_cell"] = None
        elif tag == "tr" and self._stack:
            t = self._stack[-1]
            if t["cur_row"]:
                t["rows"].append(t["cur_row"])
            t["cur_row"] = None
        elif tag == "table" and self._stack:
            self.tables.append({"rows": self._stack.pop()["rows"]})

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if self._skip:
            return
        if self._stack and self._stack[-1]["cur_cell"] is not None:
            self._stack[-1]["cur_cell"].append(data)


def extract_fields(text: str, rules: dict) -> dict:
    """按规则抽取字段。rules = {"title": bool, "tables": bool,
    "fields": [{"name": ..., "pattern": 正则}]}"""
    out = {}
    parser = _HTMLExtract()
    if rules.get("title") or rules.get("tables"):
        try:
            parser.feed(text)
            parser.close()
        except Exception:
            pass  # HTMLParser 对畸形 HTML 容错,保留已收集的部分结果
    if rules.get("title"):
        out["title"] = " ".join("".join(parser.title_parts).split()) or None
    if rules.get("tables"):
        for i, t in enumerate(parser.tables):
            t["index"] = i
        out["tables"] = parser.tables
    for fld in rules.get("fields") or []:
        name = (fld or {}).get("name")
        pattern = (fld or {}).get("pattern")
        if not name or not pattern:
            continue
        target = out.setdefault("fields", {})
        try:
            rx = re.compile(pattern, re.S)
        except re.error as exc:
            target[name] = {"error": f"bad regex: {exc}"}
            continue
        vals = []
        for m in rx.finditer(text):
            vals.append(m.group(1) if m.groups() else m.group(0))
        target[name] = vals
    return out


def diff_fields(a: dict, b: dict) -> dict:
    """比较两份抽取结果,返回逐字段差异。"""
    diff = {}
    if "title" in a or "title" in b:
        diff["title"] = {"from": a.get("title"), "to": b.get("title"),
                         "changed": a.get("title") != b.get("title")}
    if "tables" in a or "tables" in b:
        ta, tb = a.get("tables") or [], b.get("tables") or []
        tables_diff = []
        for i in range(max(len(ta), len(tb))):
            ra = [tuple(r) for r in ta[i]["rows"]] if i < len(ta) else []
            rb = [tuple(r) for r in tb[i]["rows"]] if i < len(tb) else []
            added = [list(r) for r in rb if r not in ra]
            removed = [list(r) for r in ra if r not in rb]
            tables_diff.append({"index": i, "added": added, "removed": removed,
                                "changed": bool(added or removed)})
        diff["tables"] = tables_diff
    fa, fb = a.get("fields") or {}, b.get("fields") or {}
    if fa or fb:
        fd = {}
        for k in set(fa) | set(fb):
            fd[k] = {"from": fa.get(k), "to": fb.get(k), "changed": fa.get(k) != fb.get(k)}
        diff["fields"] = fd
    return diff
