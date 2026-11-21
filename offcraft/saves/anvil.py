"""Anvil 区域文件（``.mca``）读取。

一个区域文件覆盖 32×32 = 1024 个区块，文件本身是 4 KiB 扇区的数组：

* 前 4096 字节是偏移表，每 4 字节 = ``3 字节起始扇区 + 1 字节扇区数``；
  偏移 0 表示该区块不存在（未生成）。
* 紧随其后的 4096 字节是时间戳表（int32 Unix 秒）。
* 每个区块从 ``扇区号 × 4096`` 开始：``int32 长度 L`` + ``1 字节压缩类型``
  + ``L-1`` 字节压缩数据，尾部补齐到扇区边界。

坑（都会导致"读飞"或读不到区块，代码里逐个处理了）：

* ``L`` **包含**压缩类型那 1 字节；
* 扇区数是 1 字节，所以历史上的哨兵值是 **255**：此时真实长度写在该区块
  数据之后的 4 字节里（1.13 之前的大区块常见）；
* 不能跨扇区——占用扇区数 = ``ceil((4 + L) / 4096)``；
* 压缩类型 4 是 **LZ4 block**（不是 LZ4 frame，没有 ``04 22 4D 18`` 帧头）。
"""

from __future__ import annotations

import gzip
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from offcraft.saves.nbt import NBTError, read_nbt

__all__ = [
    "COMPRESSION_GZIP",
    "COMPRESSION_LZ4",
    "COMPRESSION_LZMA",
    "COMPRESSION_NONE",
    "COMPRESSION_ZLIB",
    "RegionFile",
    "RegionFormatError",
    "parse_region_name",
    "read_region",
]

SECTOR = 4096
#: 偏移表 + 时间戳表
HEADER = 8192

COMPRESSION_GZIP = 1
COMPRESSION_ZLIB = 2
COMPRESSION_NONE = 3
COMPRESSION_LZ4 = 4
COMPRESSION_LZMA = 127

#: 扇区数 == 255 是"超大区块"的哨兵值
_OVERSIZED = 255


class RegionFormatError(ValueError):
    """区域文件损坏或结构非法。"""


def _lz4_block_decompress(raw: bytes, size_hint: int) -> bytes:
    """解 LZ4 **block** 格式（区域文件用的是这个，不是 LZ4 frame）。"""
    try:
        import lz4.block as lz4_block  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - 只在装了 lz4 的机器上走到
        raise RegionFormatError("区块使用 LZ4 压缩，需要安装 lz4 才能读取") from exc
    return lz4_block.decompress(raw, uncompressed_size=max(size_hint, 0))


def decompress_chunk(kind: int, raw: bytes) -> bytes:
    """按压缩类型解区块负载。"""
    if kind == COMPRESSION_GZIP:
        return gzip.decompress(raw)
    if kind == COMPRESSION_ZLIB:
        return zlib.decompress(raw)
    if kind == COMPRESSION_NONE:
        return raw
    if kind == COMPRESSION_LZ4:
        return _lz4_block_decompress(raw, len(raw) * 4)
    if kind == COMPRESSION_LZMA:
        try:
            import lzma
        except ImportError as exc:  # pragma: no cover
            raise RegionFormatError("区块使用 LZMA 压缩，当前环境没有 lzma") from exc
        return lzma.decompress(raw)
    raise RegionFormatError(f"未知的压缩类型：{kind}")


def parse_region_name(path: Path | str) -> tuple[int, int] | None:
    """从 ``r.<x>.<z>.mca`` 解析区域坐标；名字不符返回 ``None``。"""
    name = Path(path).name
    if not name.startswith("r.") or not name.endswith(".mca"):
        return None
    parts = name[2:-4].split(".")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


@dataclass(slots=True)
class RegionFile:
    """一个区域文件的只读视图。"""

    path: Path
    rx: int
    rz: int
    size: int

    @classmethod
    def open(cls, path: Path | str) -> "RegionFile":
        path = Path(path)
        coords = parse_region_name(path)
        if coords is None:
            raise RegionFormatError(f"不是合法的区域文件名：{path.name}")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise RegionFormatError(f"读不到区域文件：{exc}") from exc
        return cls(path=path, rx=coords[0], rz=coords[1], size=size)

    @property
    def is_empty(self) -> bool:
        """是不是"退化区域文件"——游戏会先建好 0 字节的 ``.mca`` 再往里写。

        未探索区域里的 ``poi`` / ``entities`` 目录几乎全是这种文件（实测
        真实存档：``poi/r.-1.-1.mca`` 就是 0 字节）。连 8 KiB 头部都装不下
        的文件不可能含任何区块，读它不算错误，必须当作"空"处理，否则整个
        扫描会因为一个空文件挂掉。
        """
        return self.size < HEADER

    # ------------------------------------------------------------------ 头部
    def _header(self) -> bytes:
        """返回 8 KiB 的头部；短于 4 字节的当作空表（而不是抛错）。"""
        with open(self.path, "rb") as fh:
            head = fh.read(HEADER)
        if len(head) < HEADER:
            # 文件比头部还短（含 0 字节文件）：把缺失部分当全 0，即"没有任何区块"。
            # 这里不能抛错：0 字节 .mca 是游戏正常留下的产物。
            head = head + b"\x00" * (HEADER - len(head))
        return head

    def read_header(self) -> bytes:
        return self._header()

    @staticmethod
    def index_of(cx: int, cz: int) -> int:
        """区块在偏移表里的下标（等价于 Java 的 ``(x & 31) + (z & 31) * 32``）。"""
        return (cx & 31) + (cz & 31) * 32

    @classmethod
    def entry(cls, header: bytes, cx: int, cz: int) -> tuple[int, int]:
        """返回 ``(起始扇区, 扇区数)``；起始扇区 0 表示区块不存在。"""
        index = cls.index_of(cx, cz) * 4
        offset = int.from_bytes(header[index : index + 3], "big")
        return offset, header[index + 3]

    @classmethod
    def timestamp(cls, header: bytes, cx: int, cz: int) -> int:
        index = HEADER // 2 + cls.index_of(cx, cz) * 4
        return struct.unpack_from(">i", header, index)[0] if len(header) >= index + 4 else 0

    def chunk_coords(self) -> list[tuple[int, int]]:
        """该区域文件里**存在**的区块世界坐标 ``(cx, cz)`` 列表。"""
        header = self._header()
        out: list[tuple[int, int]] = []
        for index in range(1024):
            base = index * 4
            if int.from_bytes(header[base : base + 3], "big") == 0:
                continue
            lx, lz = index & 31, index >> 5
            out.append((self.rx * 32 + lx, self.rz * 32 + lz))
        return out

    def has_chunk(self, cx: int, cz: int) -> bool:
        return self.entry(self._header(), cx, cz)[0] != 0

    # ------------------------------------------------------------------ 负载
    def read_chunk_bytes(self, cx: int, cz: int) -> bytes | None:
        """返回**已解压**的区块 NBT 字节；区块不存在返回 ``None``。

        每次调用都会重新读头部（8 KiB），因为 ``RegionFile`` 是轻量的值对象、
        没有打开的句柄要管理。扫描时用 :meth:`chunks` 迭代器可以复用同一个
        头部与文件句柄，避免 3800 次冗余 open（实测省下约 1 秒 + 明显更少的
        IO 抖动）。
        """
        header = self._header()
        start_sector, sector_count = self.entry(header, cx, cz)
        if start_sector == 0:
            return None
        with open(self.path, "rb") as fh:
            return self._read_at(fh, header, cx, cz, start_sector, sector_count)

    def _read_at(
        self,
        fh,
        header: bytes,
        cx: int,
        cz: int,
        start_sector: int,
        sector_count: int,
    ) -> bytes | None:
        if start_sector == 0:
            return None
        start = start_sector * SECTOR
        fh.seek(start)
        length_raw = fh.read(4)
        if len(length_raw) < 4:
            raise RegionFormatError(f"区块 ({cx},{cz}) 长度字段缺失")
        length = struct.unpack(">i", length_raw)[0]
        if length <= 0 or length > self.size:
            raise RegionFormatError(f"区块 ({cx},{cz}) 长度非法：{length}")
        kind_raw = fh.read(1)
        if not kind_raw:
            raise RegionFormatError(f"区块 ({cx},{cz}) 压缩类型缺失")
        kind = kind_raw[0]
        payload = fh.read(length - 1)
        if len(payload) < length - 1:
            raise RegionFormatError(f"区块 ({cx},{cz}) 数据被截断")
        if sector_count == _OVERSIZED:
            # 超大区块：真实长度写在数据之后的 4 字节里，数据本身是 4 KiB 对齐容量的
            fh.seek(start + 4 + length)
            extra = fh.read(4)
            if len(extra) == 4:
                real = struct.unpack(">i", extra)[0]
                if 0 < real <= self.size:
                    payload = payload[: max(real - 1, 0)] or payload
        return decompress_chunk(kind, payload)

    def chunks(self):
        """迭代 ``(cx, cz, 区块树)``；复用同一个文件句柄与头部。

        单个坏区块会被跳过而不是中断整个迭代——存档里一个损坏区块不该让
        整个世界的统计失败。
        """
        header = self._header()
        try:
            handle = open(self.path, "rb")
        except OSError:
            return
        with handle as fh:
            for index in range(1024):
                base = index * 4
                start_sector = int.from_bytes(header[base : base + 3], "big")
                if start_sector == 0:
                    continue
                sector_count = header[base + 3]
                lx, lz = index & 31, index >> 5
                cx = self.rx * 32 + lx
                cz = self.rz * 32 + lz
                try:
                    raw = self._read_at(fh, header, cx, cz, start_sector, sector_count)
                    if raw is None:
                        continue
                    yield cx, cz, read_nbt(raw)
                except (RegionFormatError, NBTError, OSError):
                    continue

    def read_chunk(self, cx: int, cz: int) -> dict | None:
        """返回解好的区块 NBT 树；区块不存在返回 ``None``。"""
        raw = self.read_chunk_bytes(cx, cz)
        if raw is None:
            return None
        try:
            return read_nbt(raw)
        except NBTError as exc:
            raise RegionFormatError(f"区块 ({cx},{cz}) NBT 解析失败：{exc}") from exc


def read_region(path: Path | str) -> list[tuple[int, int, dict]]:
    """把整个区域文件解成一串 ``(cx, cz, 区块树)``；单个坏区块不影响其它区块。"""
    region = RegionFile.open(path)
    out: list[tuple[int, int, dict]] = []
    for cx, cz in region.chunk_coords():
        try:
            chunk = region.read_chunk(cx, cz)
        except (RegionFormatError, OSError, OSError):
            continue
        if chunk is not None:
            out.append((cx, cz, chunk))
    return out
