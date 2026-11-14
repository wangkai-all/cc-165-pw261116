"""生成合成 WARC 档案,覆盖:重复正文、revisit(有效/缺失引用)、
损坏记录(CRC 错误、非 gzip 垃圾、截断 member)、字符集问题、gzip+chunked 正文。

用法: python3 -m warcview.fixtures <输出路径.warc.gz>
"""
from __future__ import annotations

import sys
import zlib
from hashlib import sha256

# ---------------------------------------------------------------- 基础构造

def _gzip_member(data: bytes) -> bytes:
    """单个 gzip member(wbits=31 每次产生一个完整 member)。"""
    c = zlib.compressobj(6, zlib.DEFLATED, 31)
    return c.compress(data) + c.flush()


def _http_response(body: bytes, content_type="text/html; charset=utf-8",
                   extra_headers=(), chunked=False, content_encoding=None):
    """构造 HTTP 响应内容块,返回 (content_bytes, wire_payload_bytes)。"""
    payload = body
    headers = ["HTTP/1.1 200 OK", f"Content-Type: {content_type}"]
    headers.extend(extra_headers)
    if content_encoding == "gzip":
        payload = _gzip_member(payload)
        headers.append("Content-Encoding: gzip")
    if chunked:
        headers.append("Transfer-Encoding: chunked")
        wire = (f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n")
    else:
        headers.append(f"Content-Length: {len(payload)}")
        wire = payload
    head = "\r\n".join(headers).encode("iso-8859-1") + b"\r\n\r\n"
    return head + wire, payload


def _warc_record(headers: dict, content: bytes) -> bytes:
    lines = ["WARC/1.0"] + [f"{k}: {v}" for k, v in headers.items()]
    return "\r\n".join(lines).encode("iso-8859-1") + b"\r\n\r\n" + content + b"\r\n\r\n"


def _response_record(rid, url, date, body, **kw):
    content, payload = _http_response(body, **kw)
    digest = "sha256:" + sha256(payload).hexdigest()
    headers = {
        "WARC-Type": "response",
        "WARC-Target-URI": url,
        "WARC-Date": date,
        "WARC-Record-ID": f"<urn:uuid:{rid}>",
        "Content-Type": "application/http; msgtype=response",
        "Content-Length": str(len(content)),
        "WARC-Payload-Digest": digest,
    }
    return _warc_record(headers, content), digest


def _revisit_record(rid, url, date, refers_to, refers_date, digest):
    headers = {
        "WARC-Type": "revisit",
        "WARC-Target-URI": url,
        "WARC-Date": date,
        "WARC-Record-ID": f"<urn:uuid:{rid}>",
        "WARC-Profile": "http://netpreserve.org/warc/1.0/revisit/identical-payload-digest",
        "WARC-Refers-To": f"<urn:uuid:{refers_to}>",
        "WARC-Refers-To-Date": refers_date,
        "WARC-Payload-Digest": digest,
        "Content-Type": "application/http; msgtype=response",
        "Content-Length": "0",
    }
    return _warc_record(headers, b"")


def _uuid(n):
    return f"00000000-0000-4000-8000-{n:012d}"


# ---------------------------------------------------------------- 页面正文

PRODUCTS_URL = "http://example.com/products"
ABOUT_URL = "http://example.com/about"
DATA_URL = "http://example.com/data"

PRODUCTS_V1 = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>产品价格表</title></head>
<body>
<h1>产品价格表</h1>
<p>共 3 种商品</p>
<table id="products">
<tr><th>名称</th><th>价格</th><th>库存</th></tr>
<tr><td>苹果</td><td>3.00</td><td>120</td></tr>
<tr><td>香蕉</td><td>2.50</td><td>80</td></tr>
<tr><td>橙子</td><td>4.00</td><td>60</td></tr>
</table>
<p>更新时间: 2024-01</p>
</body></html>
""".encode("utf-8")

PRODUCTS_V3 = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>产品价格表(2024 春季)</title></head>
<body>
<h1>产品价格表</h1>
<p>共 4 种商品</p>
<table id="products">
<tr><th>名称</th><th>价格</th><th>库存</th></tr>
<tr><td>苹果</td><td>3.20</td><td>100</td></tr>
<tr><td>香蕉</td><td>2.50</td><td>80</td></tr>
<tr><td>橙子</td><td>4.00</td><td>0</td></tr>
<tr><td>西瓜</td><td>5.50</td><td>30</td></tr>
</table>
<p>更新时间: 2024-04</p>
</body></html>
""".encode("utf-8")

ABOUT_LATIN1 = ("<!DOCTYPE html>\n<html><head><title>À propos</title></head>"
                "<body><p>Café naïve - notre équipe</p></body></html>\n"
                ).encode("iso-8859-1")

DATA_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>访问统计</title></head>
<body><h1>访问统计</h1>
<table><tr><th>日期</th><th>PV</th><th>UV</th></tr>
<tr><td>2024-03-01</td><td>1024</td><td>512</td></tr>
<tr><td>2024-03-02</td><td>2048</td><td>1000</td></tr>
</table></body></html>
""".encode("utf-8")

CORRUPT_HTML = b"<html><body><p>this capture is damaged</p></body></html>"

# 各记录的固定 UUID(可复现)
RID = {
    "warcinfo": _uuid(1),
    "v1": _uuid(2),
    "v2": _uuid(3),
    "revisit_ok": _uuid(4),
    "about_ok": _uuid(5),
    "corrupt": _uuid(6),
    "v3": _uuid(7),
    "revisit_missing": _uuid(8),
    "about_bad": _uuid(9),
    "data": _uuid(10),
    "truncated": _uuid(11),
}
MISSING_REF = _uuid(999)


def build_fixture_bytes() -> bytes:
    parts = []

    # 1. warcinfo
    info_content = (b"software: warcview-fixture 1.0\r\n"
                    b"format: WARC File Format 1.0\r\n"
                    b"description: synthetic archive with duplicates, "
                    b"revisits, missing refs and corrupt records\r\n")
    parts.append(_gzip_member(_warc_record({
        "WARC-Type": "warcinfo",
        "WARC-Date": "2024-01-01T00:00:00Z",
        "WARC-Record-ID": f"<urn:uuid:{RID['warcinfo']}>",
        "WARC-Filename": "sample.warc.gz",
        "Content-Type": "application/warc-fields",
        "Content-Length": str(len(info_content)),
    }, info_content)))

    # 2. products v1
    rec_v1, digest_v1 = _response_record(RID["v1"], PRODUCTS_URL,
                                         "2024-01-01T10:00:00Z", PRODUCTS_V1)
    parts.append(_gzip_member(rec_v1))

    # 3. products v2 —— 与 v1 完全相同的正文(重复捕获,不同时间)
    rec_v2, _ = _response_record(RID["v2"], PRODUCTS_URL,
                                 "2024-02-01T10:00:00Z", PRODUCTS_V1)
    parts.append(_gzip_member(rec_v2))

    # 4. products revisit —— 有效引用 v2
    parts.append(_gzip_member(_revisit_record(
        RID["revisit_ok"], PRODUCTS_URL, "2024-03-01T10:00:00Z",
        RID["v2"], "2024-02-01T10:00:00Z", digest_v1)))

    # 5. about —— 正确声明 iso-8859-1
    rec_about, _ = _response_record(RID["about_ok"], ABOUT_URL,
                                    "2024-01-15T09:00:00Z", ABOUT_LATIN1,
                                    content_type="text/html; charset=iso-8859-1")
    parts.append(_gzip_member(rec_about))

    # 6. 损坏记录:gzip CRC 错误(翻转压缩数据中间一个字节)
    rec_corrupt, _ = _response_record(RID["corrupt"], "http://example.com/corrupt",
                                      "2024-01-20T00:00:00Z", CORRUPT_HTML)
    bad = bytearray(_gzip_member(rec_corrupt))
    bad[len(bad) // 2] ^= 0xFF
    parts.append(bytes(bad))

    # 7. products v3 —— 标题与表格都有变化
    rec_v3, _ = _response_record(RID["v3"], PRODUCTS_URL,
                                 "2024-04-01T10:00:00Z", PRODUCTS_V3)
    parts.append(_gzip_member(rec_v3))

    # 8. 非 gzip 垃圾字节(模拟档案中混入的损坏数据)
    parts.append(b"\x00GARBAGE-NOT-A-MEMBER\xff\x00")

    # 9. products revisit —— 引用不存在的记录(缺失引用)
    parts.append(_gzip_member(_revisit_record(
        RID["revisit_missing"], PRODUCTS_URL, "2024-05-01T10:00:00Z",
        MISSING_REF, "2024-04-15T10:00:00Z", digest_v1)))

    # 10. about —— 声明 utf-8 但正文是 latin-1 字节(字符集处理失败路径)
    rec_bad_cs, _ = _response_record(RID["about_bad"], ABOUT_URL,
                                     "2024-02-15T09:00:00Z", ABOUT_LATIN1,
                                     content_type="text/html; charset=utf-8")
    parts.append(_gzip_member(rec_bad_cs))

    # 11. data —— Content-Encoding: gzip + Transfer-Encoding: chunked
    rec_data, _ = _response_record(RID["data"], DATA_URL,
                                   "2024-03-10T12:00:00Z", DATA_HTML,
                                   content_encoding="gzip", chunked=True)
    parts.append(_gzip_member(rec_data))

    # 12. 截断的尾部 member(模拟不完整的记录)
    rec_trunc, _ = _response_record(RID["truncated"], PRODUCTS_URL,
                                    "2024-06-01T10:00:00Z", PRODUCTS_V3)
    member = _gzip_member(rec_trunc)
    parts.append(member[:len(member) * 2 // 3])

    return b"".join(parts)


# 期望扫描结果:有效记录数 / 损坏条目数
EXPECTED_OK = 9
EXPECTED_ERRORS = 3


def write_fixture(path):
    data = build_fixture_bytes()
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "sample.warc.gz"
    n = write_fixture(out)
    print(f"wrote {out} ({n} bytes)")
