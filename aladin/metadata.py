"""去掉产物里内嵌的生成信息。

ComfyUI 默认把完整 workflow（含提示词原文）写进文件：PNG 是 `prompt`/`workflow` 文本块，
WebM 是 Matroska 的 Tags。文件一旦被下载或转发，任何人都能读出提示词。

两种格式都做**无损**处理，不重新编码：
- PNG：丢掉 tEXt / zTXt / iTXt 块，其余块原样保留（CRC 不变）。
- WebM：把 Segment 下的 Tags 元素原地改写成等长的 Void 并清零，文件里其余字节的偏移都不动，
  SeekHead / Cues 仍然有效。
解析不了就原样返回——宁可留着元数据，也不能弄坏文件。
"""
from __future__ import annotations

PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
PNG_TEXT_CHUNKS = {b'tEXt', b'zTXt', b'iTXt'}
EBML_ID = 0x1A45DFA3
SEGMENT_ID = 0x18538067
TAGS_ID = 0x1254C367
VOID_ID = 0xEC


def strip(data: bytes, name: str) -> bytes:
    lower = name.lower()
    try:
        if lower.endswith('.png'):
            return strip_png(data)
        if lower.endswith('.webm'):
            return strip_webm(data)
    except (IndexError, ValueError):
        pass
    return data


def strip_png(data: bytes) -> bytes:
    if not data.startswith(PNG_SIGNATURE):
        return data
    out = bytearray(PNG_SIGNATURE)
    i = len(PNG_SIGNATURE)
    while i < len(data):
        length = int.from_bytes(data[i:i + 4], 'big')
        kind = data[i + 4:i + 8]
        end = i + 12 + length
        if end > len(data):
            raise ValueError('PNG 块越界')
        if kind not in PNG_TEXT_CHUNKS:
            out += data[i:end]
        i = end
        if kind == b'IEND':
            break
    return bytes(out)


def _vint(data: bytes, i: int, keep_marker: bool = False) -> tuple[int, int]:
    """EBML 变长整数：返回 (值, 字节数)。ID 保留长度标记位，size 去掉。"""
    first = data[i]
    length, mask = 1, 0x80
    while length <= 8 and not first & mask:
        length += 1
        mask >>= 1
    if length > 8:
        raise ValueError('非法 EBML vint')
    value = first if keep_marker else first & (mask - 1)
    for k in range(1, length):
        value = (value << 8) | data[i + k]
    return value, length


def _unknown(size: int, length: int) -> bool:
    return size == (1 << (7 * length)) - 1


def _void(total: int) -> bytes:
    """一个总长恰为 total 字节的 Void 元素：1 字节 ID + 8 字节 size + 零填充。"""
    if total < 9:
        raise ValueError('元素太短，无法等长替换')
    payload = total - 9
    return bytes([VOID_ID, 0x01]) + payload.to_bytes(7, 'big') + bytes(payload)


def strip_webm(data: bytes) -> bytes:
    element, id_len = _vint(data, 0, keep_marker=True)
    if element != EBML_ID:
        return data
    out = bytearray(data)
    i = 0
    while i < len(data):
        element, id_len = _vint(data, i, keep_marker=True)
        size, size_len = _vint(data, i + id_len)
        body = i + id_len + size_len
        if element != SEGMENT_ID:
            i = body + size
            continue
        end = len(data) if _unknown(size, size_len) else min(len(data), body + size)
        j = body
        while j < end:
            child, child_id_len = _vint(data, j, keep_marker=True)
            child_size, child_size_len = _vint(data, j + child_id_len)
            if _unknown(child_size, child_size_len):
                break                       # 未知长度的子元素（流式 Cluster）：后面不再有 Tags 可定位
            total = child_id_len + child_size_len + child_size
            if child == TAGS_ID:
                out[j:j + total] = _void(total)
            j += total
        break
    return bytes(out)
