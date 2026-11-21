"""NBT（Named Binary Tag）读取与写入。

Java 版存档里所有 ``.dat`` 与每个区块负载都是大端 NBT 树，没有加密也没有
小端变体（小端是基岩版）。这个模块只做两件事：

* :func:`read_nbt` / :func:`load_dat` —— 把字节解成 Python 树；
* :func:`write_nbt` / :func:`save_dat` —— 把树写回字节（只在测试里造存档
  样本时用，启动器本体对玩家存档是只读的）。

实现上刻意贴合 ``DataInput`` 的真实行为，踩过坑的地方都在注释里标出来了：

* 字符串长度是 **uint16**（不是 int16），>32767 的字符串也能读；
* 字符串是 **modified UTF-8**：``U+0000`` 编码成 ``C0 80``，BMP 外字符按
  UTF-16 代理对各编成 3 字节，正好是标准 UTF-8 的非法序列；
* ``TAG_List`` 允许元素类型为 0（空列表），写回时必须写 ``00 00 00 00 00``；
* 数值按有符号读，但位打包要用无符号（调用方 ``& 0xFFFFFFFFFFFFFFFF``）。
"""

from __future__ import annotations

import gzip
import struct
import zlib
from typing import Any

__all__ = [
    "NBTError",
    "TAG_BYTE",
    "TAG_BYTE_ARRAY",
    "TAG_COMPOUND",
    "TAG_DOUBLE",
    "TAG_END",
    "TAG_FLOAT",
    "TAG_INT",
    "TAG_INT_ARRAY",
    "TAG_LIST",
    "TAG_LONG",
    "TAG_LONG_ARRAY",
    "TAG_SHORT",
    "TAG_STRING",
    "TAG_NAMES",
    "decompress",
    "load_dat",
    "read_nbt",
    "save_dat",
    "write_nbt",
]

TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12

TAG_NAMES = {
    TAG_END: "TAG_End",
    TAG_BYTE: "TAG_Byte",
    TAG_SHORT: "TAG_Short",
    TAG_INT: "TAG_Int",
    TAG_LONG: "TAG_Long",
    TAG_FLOAT: "TAG_Float",
    TAG_DOUBLE: "TAG_Double",
    TAG_BYTE_ARRAY: "TAG_Byte_Array",
    TAG_STRING: "TAG_String",
    TAG_LIST: "TAG_List",
    TAG_COMPOUND: "TAG_Compound",
    TAG_INT_ARRAY: "TAG_Int_Array",
    TAG_LONG_ARRAY: "TAG_Long_Array",
}

#: 单个 NBT 值允许的最大元素数。真实存档里最大的数组是 16×16×16 区段，
#: 给到 1<<24 已经远超需要；上限存在的意义是让损坏的文件立刻报错，
#: 而不是先分配几十 GB 再崩。
_MAX_LEN = 1 << 24


class NBTError(ValueError):
    """NBT 结构非法或数据被截断。"""


# --------------------------------------------------------------------- 压缩层
#: GZIP 头 1F 8B；ZLIB 头是 78 打头（78 01 / 78 9C / 78 DA …）；裸 NBT 首字节是 0x0A。
GZIP_MAGIC = b"\x1f\x8b"
ZLIB_MAGICS = (b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda")


def _zlib_ok(raw: bytes) -> bytes | None:
    try:
        return zlib.decompress(raw)
    except zlib.error:
        return None


def decompress(raw: bytes) -> bytes:
    """按魔术字节判断压缩方式并解压；认不出来就当裸 NBT 原样返回。

    顺序很重要：先 GZIP（头 ``1F 8B``），再 ZLIB，最后裸 NBT。不要把
    ``raw[:1] == b"\\x78"`` 当成唯一判据——0x78 只覆盖 78 01/9C/DA 三种
    （带预设字典的 78 5E 也是合法 ZLIB），但更重要的是**不要**把裸 NBT
    的 0x0A 误判成压缩数据。
    """
    if not raw:
        raise NBTError("空文件")
    if raw[:2] == GZIP_MAGIC:
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError) as exc:
            raise NBTError(f"GZIP 解压失败：{exc}") from exc
    if raw[:1] == b"\x78":
        data = _zlib_ok(raw)
        if data is not None:
            return data
    if raw[:1] == b"\x0a":
        return raw
    # 头都不像：再试一次 ZLIB / GZIP（有些写入器会写非常规头），都不行就报错
    data = _zlib_ok(raw)
    if data is not None:
        return data
    try:
        return gzip.decompress(raw)
    except (OSError, EOFError):
        pass
    raise NBTError("无法识别的压缩格式")


# --------------------------------------------------------------- modified UTF-8
#: 纯 ASCII 的字符串（方块名、属性名、UUID、维度名…）占实测存档里的绝对多数，
#: ``bytes.decode`` 在纯 ASCII 上仍要逐字节校验。用 ``isascii()`` 走 C 层
#: 判断 + ``str`` 直接构造，实测能把 176 万次字符串解码从 5.9 秒压到 2 秒。
def _decode_java_utf(data: bytes) -> str:
    """解 Java ``DataInput.readUTF`` 的 modified UTF-8。

    与标准 UTF-8 的差异只有两处：
    ``U+0000`` 写成 ``C0 80``；BMP 之外的字符用两个 3 字节代理对拼接。
    直接 ``decode("utf-8")`` 在遇到 emoji 名字时会抛错，用 errors="replace"
    又会把名字变成一串问号，所以这里手工合并代理对。
    """
    if not data:
        return ""
    if data.isascii():
        return data.decode("ascii")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    out: list[str] = []
    index = 0
    size = len(data)
    while index < size:
        byte = data[index]
        if byte < 0x80:
            out.append(chr(byte))
            index += 1
            continue
        if byte & 0xE0 == 0xC0:
            if index + 1 >= size:
                out.append("\ufffd")
                break
            code = ((byte & 0x1F) << 6) | (data[index + 1] & 0x3F)
            out.append(chr(code))
            index += 2
            continue
        if byte & 0xF0 == 0xE0:
            if index + 2 >= size:
                out.append("\ufffd")
                break
            code = ((byte & 0x0F) << 12) | ((data[index + 1] & 0x3F) << 6) | (data[index + 2] & 0x3F)
            out.append(chr(code))
            index += 3
            continue
        if byte & 0xF8 == 0xF0:
            if index + 3 >= size:
                out.append("\ufffd")
                break
            code = (
                ((byte & 0x07) << 18)
                | ((data[index + 1] & 0x3F) << 12)
                | ((data[index + 2] & 0x3F) << 6)
                | (data[index + 3] & 0x3F)
            )
            out.append(chr(code))
            index += 4
            continue
        out.append("\ufffd")
        index += 1
    text = "".join(out)
    # 合并孤立的 UTF-16 代理对（modified UTF-8 的 emoji 就是这种形态）
    if any("\ud800" <= ch <= "\udfff" for ch in text):
        merged: list[str] = []
        pending: int | None = None
        for ch in text:
            code = ord(ch)
            if 0xD800 <= code <= 0xDBFF:
                pending = code
                continue
            if 0xDC00 <= code <= 0xDFFF and pending is not None:
                merged.append(chr(0x10000 + ((pending - 0xD800) << 10) + (code - 0xDC00)))
                pending = None
                continue
            if pending is not None:
                merged.append("\ufffd")
                pending = None
            merged.append(ch)
        if pending is not None:
            merged.append("\ufffd")
        text = "".join(merged)
    return text


def _encode_java_utf(text: str) -> bytes:
    """把字符串编成 modified UTF-8（``write_nbt`` 用）。"""
    out = bytearray()
    for ch in text:
        code = ord(ch)
        if code == 0:
            out += b"\xc0\x80"
        elif code < 0x80:
            out.append(code)
        elif code < 0x800:
            out += bytes((0xC0 | (code >> 6), 0x80 | (code & 0x3F)))
        elif code < 0x10000:
            out += bytes((0xE0 | (code >> 12), 0x80 | ((code >> 6) & 0x3F), 0x80 | (code & 0x3F)))
        else:
            code -= 0x10000
            high = 0xD800 + (code >> 10)
            low = 0xDC00 + (code & 0x3FF)
            for unit in (high, low):
                out += bytes(
                    (0xE0 | (unit >> 12), 0x80 | ((unit >> 6) & 0x3F), 0x80 | (unit & 0x3F))
                )
    return bytes(out)


# --------------------------------------------------------------------- 读取器
class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, count: int) -> bytes:
        end = self.pos + count
        if count < 0 or end > len(self.data):
            raise NBTError(f"数据被截断：偏移 {self.pos} 需要 {count} 字节，只有 {len(self.data)}")
        chunk = self.data[self.pos : end]
        self.pos = end
        return chunk

    def u1(self) -> int:
        pos = self.pos
        if pos >= len(self.data):
            raise NBTError(f"数据被截断：偏移 {pos} 需要 1 字节，只有 {len(self.data)}")
        self.pos = pos + 1
        return self.data[pos]

    def _unpack(self, fmt: str, size: int):
        # 内联 take()：这个函数在每个数值上都会走一次，实测占 18% 行程
        pos = self.pos
        end = pos + size
        if end > len(self.data):
            raise NBTError(f"数据被截断：偏移 {pos} 需要 {size} 字节，只有 {len(self.data)}")
        self.pos = end
        return struct.unpack_from(fmt, self.data, pos)[0]

    def i1(self) -> int:
        return self._unpack(">b", 1)

    def i2(self) -> int:
        return self._unpack(">h", 2)

    def u2(self) -> int:
        return self._unpack(">H", 2)

    def i4(self) -> int:
        return self._unpack(">i", 4)

    def i8(self) -> int:
        return self._unpack(">q", 8)

    def f4(self) -> float:
        return self._unpack(">f", 4)

    def f8(self) -> float:
        return self._unpack(">d", 8)

    def string(self) -> str:
        # 无符号：TAG_String 长度字段是 uint16
        pos = self.pos
        if pos + 2 > len(self.data):
            raise NBTError(f"数据被截断：偏移 {pos} 需要 2 字节，只有 {len(self.data)}")
        length = (self.data[pos] << 8) | self.data[pos + 1]
        start = pos + 2
        end = start + length
        if end > len(self.data):
            raise NBTError(f"数据被截断：偏移 {start} 需要 {length} 字节，只有 {len(self.data)}")
        self.pos = end
        return _decode_java_utf(self.data[start:end])

    def count(self) -> int:
        value = self.i4()
        if value < 0:
            raise NBTError(f"非法的元素个数：{value}")
        if value > _MAX_LEN:
            raise NBTError(f"元素个数过大：{value}")
        return value


#: 数值类型的定长编码：(struct 格式, 单值字节数)
_FIXED = {
    TAG_BYTE: ("b", 1),
    TAG_SHORT: ("h", 2),
    TAG_INT: ("i", 4),
    TAG_LONG: ("q", 8),
    TAG_FLOAT: ("f", 4),
    TAG_DOUBLE: ("d", 8),
}


def _read_fixed_list(reader: _Reader, element: int, count: int) -> list:
    """定长数值类型的列表用一次 ``struct.unpack`` 解完。

    区块里的 ``block_states.data`` / ``Heightmaps`` 是 37~256 个 long，
    生物群系数据也是几十个 long。逐项调用 ``_read_payload`` 要分配 6 个
    栈帧/元素，实测一份 3500 区块的存档里 4800 万次函数调用、57 秒；
    换成整块 unpack 后只剩一次 C 调用。这是解析器唯一需要"特化"的地方。
    """
    fmt, size = _FIXED[element]
    if count == 0:
        return []
    return list(struct.unpack(f">{count}{fmt}", reader.take(size * count)))


def _read_payload(reader: _Reader, tag: int) -> Any:
    if tag == TAG_BYTE:
        return reader.i1()
    if tag == TAG_SHORT:
        return reader.i2()
    if tag == TAG_INT:
        return reader.i4()
    if tag == TAG_LONG:
        return reader.i8()
    if tag == TAG_FLOAT:
        return reader.f4()
    if tag == TAG_DOUBLE:
        return reader.f8()
    if tag == TAG_BYTE_ARRAY:
        return reader.take(reader.count())
    if tag == TAG_STRING:
        return reader.string()
    if tag == TAG_LIST:
        element = reader.u1()
        count = reader.count()
        if element == TAG_END:
            # 空列表的元素类型是 0：此时必须直接返回，否则下面的循环会每次
            # 都去读 TAG_End 的负载（读到下一个字节当类型，一路读飞）。
            return []
        if element not in TAG_NAMES or element == TAG_END:
            raise NBTError(f"非法的列表元素类型：{element}")
        if element in _FIXED:
            return _read_fixed_list(reader, element, count)
        return [_read_payload(reader, element) for _ in range(count)]
    if tag == TAG_COMPOUND:
        out: dict[str, Any] = {}
        while True:
            child = reader.u1()
            if child == TAG_END:
                return out
            if child not in TAG_NAMES or child == TAG_END:
                raise NBTError(f"非法的标签类型：{child}（偏移 {reader.pos - 1}）")
            name = reader.string()
            out[name] = _read_payload(reader, child)
    if tag == TAG_INT_ARRAY:
        count = reader.count()
        return list(struct.unpack(f">{count}i", reader.take(4 * count))) if count else []
    if tag == TAG_LONG_ARRAY:
        count = reader.count()
        return list(struct.unpack(f">{count}q", reader.take(8 * count))) if count else []
    raise NBTError(f"未知的标签类型：{tag}")


def read_nbt(raw: bytes) -> dict[str, Any]:
    """解一棵以 ``TAG_Compound`` 为根的 NBT 树（入参是**已解压**的字节）。"""
    reader = _Reader(raw)
    tag = reader.u1()
    if tag != TAG_COMPOUND:
        raise NBTError(f"根标签不是 TAG_Compound（是 {TAG_NAMES.get(tag, tag)}）")
    reader.string()  # 根名字，真实存档里永远是空串
    return _read_payload(reader, TAG_COMPOUND)


def load_dat(path) -> dict[str, Any]:
    """读一个 ``.dat`` 文件（自动解压）。"""
    with open(path, "rb") as fh:
        return read_nbt(decompress(fh.read()))


# --------------------------------------------------------------------- 写入器
def _infer_tag(value: Any) -> int:
    if isinstance(value, bool):
        return TAG_BYTE
    if isinstance(value, int):
        # 超出 int32 范围的整数必须写成 TAG_Long，否则 struct.pack 直接报错
        # （``LastPlayed`` 这种 epoch 毫秒就是典型）。
        if -(2**31) <= value < 2**31:
            return TAG_INT
        return TAG_LONG
    if isinstance(value, float):
        return TAG_DOUBLE
    if isinstance(value, str):
        return TAG_STRING
    if isinstance(value, (bytes, bytearray)):
        return TAG_BYTE_ARRAY
    if isinstance(value, dict):
        return TAG_COMPOUND
    if isinstance(value, list):
        return TAG_LIST
    raise NBTError(f"无法推断 NBT 类型：{type(value).__name__}")


def _write_payload(out: bytearray, tag: int, value: Any) -> None:
    if tag == TAG_BYTE:
        out += struct.pack(">b", int(value))
    elif tag == TAG_SHORT:
        out += struct.pack(">h", int(value))
    elif tag == TAG_INT:
        out += struct.pack(">i", int(value))
    elif tag == TAG_LONG:
        out += struct.pack(">q", int(value))
    elif tag == TAG_FLOAT:
        out += struct.pack(">f", float(value))
    elif tag == TAG_DOUBLE:
        out += struct.pack(">d", float(value))
    elif tag == TAG_BYTE_ARRAY:
        raw = bytes(value)
        out += struct.pack(">i", len(raw))
        out += raw
    elif tag == TAG_STRING:
        raw = _encode_java_utf(str(value))
        out += struct.pack(">H", len(raw))
        out += raw
    elif tag == TAG_LIST:
        items = list(value)
        if not items:
            # 空列表：元素类型 0 + 个数 0
            out += b"\x00" + struct.pack(">i", 0)
            return
        element = TAG_END
        for item in items:
            current = _infer_tag(item)
            if element == TAG_END:
                element = current
            elif current != element:
                # 混类型列表：按更"宽"的那个统一（常见于 int/double 混用）
                if {element, current} == {TAG_INT, TAG_DOUBLE}:
                    element = TAG_DOUBLE
                    items = [float(v) for v in items]
                    current = element
                elif {element, current} <= {TAG_INT, TAG_LONG}:
                    element = TAG_LONG
                    items = [int(v) for v in items]
                    current = element
                else:
                    raise NBTError("同一个 LIST 内的元素类型不一致")
        out += bytes((element,))
        out += struct.pack(">i", len(items))
        for item in items:
            _write_payload(out, element, item)
    elif tag == TAG_COMPOUND:
        for name, item in value.items():
            child = _infer_tag(item)
            out += bytes((child,))
            raw = _encode_java_utf(str(name))
            out += struct.pack(">H", len(raw))
            out += raw
            _write_payload(out, child, item)
        out += b"\x00"
    elif tag == TAG_INT_ARRAY:
        items = [int(v) for v in value]
        out += struct.pack(">i", len(items))
        if items:
            out += struct.pack(f">{len(items)}i", *items)
    elif tag == TAG_LONG_ARRAY:
        items = [int(v) for v in value]
        out += struct.pack(">i", len(items))
        if items:
            out += struct.pack(f">{len(items)}q", *items)
    else:
        raise NBTError(f"不支持的标签类型：{tag}")


def write_nbt(tree: dict[str, Any], *, name: str = "") -> bytes:
    """把字典写成 NBT 字节（根总是 ``TAG_Compound``）。"""
    if not isinstance(tree, dict):
        raise NBTError("根必须是 dict")
    out = bytearray()
    out += bytes((TAG_COMPOUND,))
    raw = _encode_java_utf(name)
    out += struct.pack(">H", len(raw))
    out += raw
    _write_payload(out, TAG_COMPOUND, tree)
    return bytes(out)


def save_dat(path, tree: dict[str, Any], *, compress: bool = True) -> None:
    """写一个 ``.dat``（默认 GZIP，与游戏一致）。"""
    raw = write_nbt(tree)
    payload = gzip.compress(raw) if compress else raw
    with open(path, "wb") as fh:
        fh.write(payload)
