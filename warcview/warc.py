"""流式 WARC 读取:逐条记录解析,保留字节级偏移,容忍损坏记录。

设计要点:
- 压缩档案(.warc.gz)按 gzip member 逐个读取,每个 member 对应一条记录,
  member 在物理文件中的起始偏移即记录偏移,供后续随机访问。
- 非压缩档案(.warc)按记录边界顺序读取。
- 损坏记录抛出 WarcError(携带偏移),调用方记录错误后可重同步继续扫描,
  损坏位置不会被静默丢弃。
- 只读访问,绝不写入档案文件。
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

GZIP_MAGIC = b"\x1f\x8b\x08"
WARC_MAGIC = b"WARC/"
MAX_HEADER_BYTES = 1 << 20      # 头部块上限 1 MiB
MAX_RECORD_BYTES = 1 << 28      # 解压后单条记录上限 256 MiB


class WarcError(Exception):
    """解析错误。offset 为记录在档案中的起始偏移。"""

    def __init__(self, message, offset=None):
        super().__init__(message)
        self.message = message
        self.offset = offset


@dataclass
class WarcRecord:
    offset: int            # 物理文件中的起始偏移
    member_length: int     # 物理长度(gzip member 或明文记录)
    version: str
    headers: dict          # WARC 头,小写键
    content: bytes         # Content-Length 指定的内容块
    content_offset: int    # 内容块在(解压后)记录内的偏移

    @property
    def end_offset(self):
        return self.offset + self.member_length


def _read_exact(f, n, offset, what):
    buf = f.read(n)
    if len(buf) < n:
        raise WarcError(f"truncated {what}", offset)
    return buf


def read_gzip_member(f, offset):
    """读取 offset 处的单个 gzip member,返回 (解压数据, member 结束偏移)。"""
    f.seek(offset)
    hdr = f.read(10)
    if not hdr:
        raise EOFError
    if len(hdr) < 10:
        raise WarcError("truncated gzip header", offset)
    if hdr[0:2] != b"\x1f\x8b":
        raise WarcError("bad gzip magic", offset)
    if hdr[2] != 8:
        raise WarcError("unsupported gzip compression method", offset)
    flags = hdr[3]
    if flags & 0xE0:
        raise WarcError("reserved gzip flags set", offset)
    pos = offset + 10
    if flags & 0x04:  # FEXTRA
        xlen = struct.unpack("<H", _read_exact(f, 2, offset, "gzip extra"))[0]
        f.seek(xlen, 1)
        pos += 2 + xlen
    for bit in (0x08, 0x10):  # FNAME, FCOMMENT
        if flags & bit:
            while True:
                b = f.read(1)
                if not b:
                    raise WarcError("truncated gzip header string", offset)
                pos += 1
                if b == b"\x00":
                    break
    if flags & 0x02:  # FHCRC
        f.seek(2, 1)
        pos += 2
    d = zlib.decompressobj(-15)
    out = bytearray()
    while True:
        chunk = f.read(1 << 16)
        if not chunk:
            raise WarcError("truncated deflate stream", offset)
        try:
            out += d.decompress(chunk)
        except zlib.error as exc:
            raise WarcError(f"deflate error: {exc}", offset)
        if len(out) > MAX_RECORD_BYTES:
            raise WarcError("record too large", offset)
        if d.eof:
            pos += len(chunk) - len(d.unused_data)
            break
    f.seek(pos)
    trailer = _read_exact(f, 8, offset, "gzip trailer")
    crc, isize = struct.unpack("<II", trailer)
    data = bytes(out)
    if crc != (zlib.crc32(data) & 0xFFFFFFFF):
        raise WarcError("gzip CRC mismatch", offset)
    if isize != (len(data) & 0xFFFFFFFF):
        raise WarcError("gzip ISIZE mismatch", offset)
    return data, pos + 8


def parse_header_block(head: bytes, offset):
    """解析 WARC 版本行 + 头部行,返回 (version, headers)。"""
    text = head.decode("iso-8859-1")
    lines = text.splitlines()
    if not lines or not lines[0].strip().startswith("WARC/"):
        raise WarcError("missing WARC version line", offset)
    version = lines[0].strip()
    headers = {}
    last_key = None
    for raw in lines[1:]:
        if raw == "":
            continue
        if raw[0] in " \t" and last_key:  # 折叠行
            headers[last_key] += " " + raw.strip()
            continue
        if ":" not in raw:
            raise WarcError(f"malformed header line: {raw!r}", offset)
        k, v = raw.split(":", 1)
        last_key = k.strip().lower()
        headers[last_key] = v.strip()
    return version, headers


def parse_record_buffer(buf: bytes, offset: int, member_length: int) -> WarcRecord:
    """从解压后的完整记录缓冲解析一条 WARC 记录。"""
    if len(buf) > MAX_RECORD_BYTES:
        raise WarcError("record too large", offset)
    end = buf.find(b"\r\n\r\n")
    sep = 4
    if end < 0:
        end = buf.find(b"\n\n")
        sep = 2
    if end < 0:
        raise WarcError("unterminated WARC header block", offset)
    version, headers = parse_header_block(buf[:end], offset)
    cl = headers.get("content-length")
    if cl is None:
        raise WarcError("missing Content-Length", offset)
    try:
        content_length = int(cl)
    except ValueError:
        raise WarcError(f"invalid Content-Length: {cl!r}", offset)
    if content_length < 0:
        raise WarcError("negative Content-Length", offset)
    cstart = end + sep
    content = buf[cstart:cstart + content_length]
    if len(content) < content_length:
        raise WarcError("truncated record content", offset)
    return WarcRecord(offset, member_length, version, headers, content, cstart)


def read_plain_record(f, offset):
    """读取非压缩 .warc 文件中 offset 处的记录,返回 (record, 结束偏移)。"""
    f.seek(offset)
    head = bytearray()
    while True:
        line = f.readline()
        if not line:
            if not head:
                raise EOFError
            raise WarcError("unterminated WARC header block", offset)
        head += line
        if len(head) > MAX_HEADER_BYTES:
            raise WarcError("header block too large", offset)
        if line in (b"\r\n", b"\n"):
            break
    version, headers = parse_header_block(bytes(head), offset)
    cl = headers.get("content-length")
    if cl is None:
        raise WarcError("missing Content-Length", offset)
    try:
        content_length = int(cl)
    except ValueError:
        raise WarcError(f"invalid Content-Length: {cl!r}", offset)
    content = f.read(content_length)
    if len(content) < content_length:
        raise WarcError("truncated record content", offset)
    # 记录间分隔符 \r\n\r\n,容错地只消费匹配前缀
    tail = f.read(4)
    expected = b"\r\n\r\n"
    matched = 0
    for i, byte in enumerate(tail):
        if byte == expected[i]:
            matched += 1
        else:
            break
    end = f.tell() - (len(tail) - matched)
    return WarcRecord(offset, end - offset, version, headers, content, len(head)), end


def load_record_at(f, fmt, offset) -> WarcRecord:
    """随机访问:读取并解析 offset 处的记录。"""
    if fmt == "gz":
        data, end = read_gzip_member(f, offset)
        return parse_record_buffer(data, offset, end - offset)
    rec, _end = read_plain_record(f, offset)
    return rec


def sniff_format(path) -> str:
    """识别档案格式:'gz' 或 'plain'。"""
    with open(path, "rb") as f:
        start = f.read(16)
    if start[:2] == b"\x1f\x8b":
        return "gz"
    if start.startswith(WARC_MAGIC):
        return "plain"
    raise WarcError("unrecognized archive format", 0)


def find_next_member(f, start, size, fmt, limit=1 << 26):
    """重同步:从 start 起搜索下一个可能的记录起始魔数,返回候选偏移或 None。"""
    magic = GZIP_MAGIC if fmt == "gz" else b"\nWARC/"
    pos = start
    while pos < size:
        if pos - start > limit:
            return None
        f.seek(pos)
        chunk = f.read(min(1 << 20, size - pos))
        if not chunk:
            return None
        i = chunk.find(magic)
        if i >= 0:
            return pos + i + (1 if fmt == "plain" else 0)
        step = len(chunk) - 8
        if step <= 0:
            return None
        pos += step
    return None
