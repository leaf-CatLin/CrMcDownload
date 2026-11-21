"""用合成存档验证解析器（NBT → 区域文件 → 位打包 → 报告）。"""

import tempfile
import unittest
from pathlib import Path

from offcraft.saves.anvil import RegionFile, parse_region_name
from offcraft.saves.mapcolors import MAP_BASE_COLORS, decode_map_colors, map_color_rgb
from offcraft.saves.model import SaveScanOptions
from offcraft.saves.nbt import NBTError, read_nbt, write_nbt
from offcraft.saves.palette import (
    SCHEME_COMPACT,
    SCHEME_PACKED,
    decode_heightmap,
    encode_heightmap,
    pack_indices,
    palette_bits,
    section_blocks,
    unpack_indices,
)
from offcraft.saves.report import _plain_text, _uuid_from_ints, scan_save
from tests.save_fixture import build_legacy_save, build_save


class _TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class NBTTests(unittest.TestCase):
    def test_empty_list_roundtrip(self) -> None:
        tree = {"Items": []}
        self.assertEqual(read_nbt(write_nbt(tree)), tree)

    def test_empty_list_wire_format(self) -> None:
        """空列表必须写成 ``00 00 00 00 00``（类型 0 + 个数 0）。"""
        raw = write_nbt({"a": []})
        self.assertIn(b"\x09\x00\x01a\x00\x00\x00\x00\x00", raw)

    def test_modified_utf8_emoji_and_nul(self) -> None:
        tree = {"Name": "Steve😀", "Zero": "a\x00b"}
        raw = write_nbt(tree)
        # U+0000 必须编成 C0 80，emoji 必须按代理对编成两个 3 字节序列
        self.assertIn(b"\xc0\x80", raw)
        self.assertEqual(read_nbt(raw), tree)

    def test_uint16_string_length(self) -> None:
        """长度字段按 uint16 读：>32767 的字符串不能当成负数。"""
        text = "x" * 40000
        self.assertEqual(read_nbt(write_nbt({"s": text}))["s"], text)

    def test_nested_types(self) -> None:
        tree = {
            "bytes": b"\x01\x02",
            "ints": [1, 2, 3],
            "mixed": [1.5, 2.5],
            "deep": {"a": {"b": [{"c": 1}]}},
        }
        decoded = read_nbt(write_nbt(tree))
        self.assertEqual(decoded["bytes"], b"\x01\x02")
        self.assertEqual(decoded["ints"], [1, 2, 3])
        self.assertEqual(decoded["deep"], {"a": {"b": [{"c": 1}]}})

    def test_truncated_raises(self) -> None:
        raw = write_nbt({"x": 1})
        with self.assertRaises(NBTError):
            read_nbt(raw[:-1])

    def test_bad_root_rejected(self) -> None:
        with self.assertRaises(NBTError):
            read_nbt(b"\x08\x00\x00")


class PackingTests(unittest.TestCase):
    def test_does_not_cross_long_boundary(self) -> None:
        # 5 bit × 每 long 12 项：第 13 项必须从下一个 long 开始
        indices = list(range(13))
        bits = 5
        longs = pack_indices(indices, bits)
        per_long = 64 // bits
        self.assertEqual(len(longs), 2)
        self.assertEqual(unpack_indices(longs, bits, 13), indices)
        self.assertLessEqual(per_long, 12)

    def test_min_bits(self) -> None:
        self.assertEqual(palette_bits(1, min_bits=4), 4)
        self.assertEqual(palette_bits(2, min_bits=4), 4)
        self.assertEqual(palette_bits(33, min_bits=4), 6)
        self.assertEqual(palette_bits(1, min_bits=1), 1)

    def test_signed_long_roundtrip(self) -> None:
        """最高位为 1 的 long 必须能当有符号数往返。"""
        indices = [15] * 4096
        longs = pack_indices(indices, 4)
        self.assertTrue(any(value < 0 for value in longs))
        self.assertEqual(unpack_indices(longs, 4, 4096), indices)

    def test_packed_scheme(self) -> None:
        """1.16 之前的连续位流：手工拼一个横跨 long 边界的值验证解码。"""
        bits = 5
        indices = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 31]
        # 连续位流编码（与 compact 方案的区别就在"不重新对齐"）
        buffer = 0
        buffered = 0
        longs = []
        for index in indices:
            buffer |= index << buffered
            buffered += bits
            while buffered >= 64:
                longs.append(buffer & 0xFFFFFFFFFFFFFFFF)
                buffer >>= 64
                buffered -= 64
        if buffered:
            longs.append(buffer)
        signed = [value - (1 << 64) if value >= (1 << 63) else value for value in longs]
        self.assertEqual(
            unpack_indices(signed, bits, len(indices), scheme=SCHEME_PACKED), indices
        )
        # 同一个数据用新版方案解会错位——这正是"两套解码器都要有"的原因
        self.assertNotEqual(
            unpack_indices(signed, bits, len(indices), scheme=SCHEME_COMPACT), indices
        )

    def test_heightmap(self) -> None:
        values = [(index % 384) for index in range(256)]
        self.assertEqual(decode_heightmap(encode_heightmap(values)), values)

    def test_single_palette_has_no_data(self) -> None:
        palette, indices = section_blocks(
            {"block_states": {"palette": [{"Name": "minecraft:stone"}]}}
        )
        self.assertEqual(len(palette), 1)
        self.assertEqual(set(indices), {0})


class RegionTests(_TempCase):
    def test_region_name(self) -> None:
        self.assertEqual(parse_region_name("r.-1.2.mca"), (-1, 2))
        self.assertIsNone(parse_region_name("foo.mca"))

    def test_read_synthetic_region(self) -> None:
        save = build_save(self.tmp / "world")
        region = RegionFile.open(save.region)
        coords = region.chunk_coords()
        self.assertEqual(len(coords), 2)
        chunk = region.read_chunk(0, 0)
        assert chunk is not None
        self.assertEqual(chunk["xPos"], 0)
        self.assertEqual(len(chunk["sections"]), 24)
        self.assertEqual(chunk["Status"], "minecraft:full")
        # 不存在的区块返回 None
        self.assertIsNone(region.read_chunk(5, 5))

    def test_offset_table_split(self) -> None:
        """偏移表是 3 字节起始扇区 + 1 字节扇区数，必须分开读。"""
        save = build_save(self.tmp / "world")
        region = RegionFile.open(save.region)
        header = region.read_header()
        start, count = RegionFile.entry(header, 0, 0)
        self.assertGreaterEqual(start, 2)  # 前两个扇区是头部
        self.assertGreaterEqual(count, 1)
        self.assertTrue(region.has_chunk(0, 0))
        self.assertFalse(region.has_chunk(5, 5))


class MapColorTests(unittest.TestCase):
    def test_palette_size(self) -> None:
        self.assertGreaterEqual(len(MAP_BASE_COLORS), 58)

    def test_transparent(self) -> None:
        self.assertIsNone(map_color_rgb(0))
        self.assertIsNone(map_color_rgb(3))

    def test_grass_shades(self) -> None:
        # 草方块基础色 1 = 0x7FB238；明暗档 2（255/255）就是原色
        self.assertEqual(map_color_rgb(1 * 4 + 2), (127, 178, 56))
        # 明暗档 0 是 180/255
        self.assertEqual(map_color_rgb(1 * 4 + 0), (89, 125, 39))

    def test_decode_map_colors(self) -> None:
        colors = bytes([1 * 4 + 2]) * (128 * 128)
        pixels = decode_map_colors(colors)
        self.assertEqual(len(pixels), 128 * 128)
        self.assertEqual(pixels[0], (127, 178, 56, 255))


class HelperTests(unittest.TestCase):
    def test_uuid_from_ints(self) -> None:
        uid = _uuid_from_ints([0x1C1D9D4E, 0x8F5A4A6F, 0x9D1B2F2C, 0x3D4E5F60])
        self.assertEqual(uid, "1c1d9d4e-8f5a-4a6f-9d1b-2f2c3d4e5f60")
        # NBT 里的 UUID 是**有符号** int：最高位为 1 时必须是负数，截 32 位后一致
        self.assertEqual(_uuid_from_ints([0x1C1D9D4E, -1889908113, -1659162836, 0x3D4E5F60]), uid)

    def test_plain_text(self) -> None:
        self.assertEqual(_plain_text('{"text":"Blade"}'), "Blade")
        self.assertEqual(_plain_text("§cRed"), "Red")
        self.assertEqual(_plain_text("plain"), "plain")


class ScanTests(_TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.fixture = build_save(self.tmp / "存档测试世界")
        self.report = scan_save(self.fixture.root)

    def test_level_metadata(self) -> None:
        level = self.report.level
        self.assertEqual(level.level_name, "存档测试世界")
        self.assertEqual(level.data_version, 3700)
        self.assertEqual(level.game_mode_name, "生存")
        self.assertEqual(level.difficulty_name, "普通")
        self.assertTrue(level.allow_commands)
        self.assertEqual(level.seed, self.fixture.seed)
        self.assertEqual(level.time, 720000)
        assert level.spawn is not None
        self.assertEqual(level.spawn.block, (8, 65, 8))

    def test_split_world_files(self) -> None:
        self.assertTrue(self.report.level.split_world_data)
        names = {rule.short_name for rule in self.report.rules}
        self.assertIn("keep_inventory", names)
        self.assertIn("random_tick_speed", names)
        assert self.report.weather is not None
        self.assertTrue(self.report.weather.raining)
        assert self.report.border is not None
        self.assertAlmostEqual(self.report.border.size, 29999984.0)
        self.assertEqual(len(self.report.clocks), 2)

    def test_chunks(self) -> None:
        self.assertEqual(self.report.total_chunks, 2)
        self.assertEqual(self.report.full_chunks, 2)
        top = dict(self.report.chunks.top_blocks)
        self.assertIn("minecraft:stone", top)
        self.assertIn("minecraft:grass_block", top)
        self.assertEqual(self.report.chunks.min_ground, self.fixture.ground_y)
        self.assertEqual(self.report.chunks.max_ground, self.fixture.ground_y)
        biomes = dict(self.report.chunks.biomes)
        self.assertIn("minecraft:plains", biomes)

    def test_containers(self) -> None:
        # 合成存档里有两口箱子：一口装了东西，一口是未打开的战利品箱
        self.assertEqual(len(self.report.containers), 2)
        chest = next(c for c in self.report.containers if c.items)
        self.assertEqual(chest.kind_name, "箱子")
        self.assertEqual(len(chest.items), self.fixture.chest_item_count)
        sword = next(item for item in chest.items if "sword" in item.item_id)
        self.assertEqual(sword.custom_name, "Blade")
        self.assertEqual(sword.damage, 12)
        self.assertEqual(sword.enchantments, (("minecraft:sharpness", 5),))

    def test_loot_pending_container(self) -> None:
        """未打开的战利品箱不能被当成"空箱子"。"""
        pending = [c for c in self.report.containers if c.is_loot_pending]
        self.assertEqual(len(pending), 1)
        box = pending[0]
        self.assertEqual(box.items, ())
        self.assertEqual(box.loot_table, "minecraft:chests/simple_dungeon")
        self.assertIsNotNone(box.loot_seed)
        self.assertEqual(box.state_label, "未打开（内容未生成）")

    def test_entities_and_poi(self) -> None:
        groups = {group.entity_id: group.count for group in self.report.entities}
        self.assertEqual(groups["minecraft:zombie"], 2)
        self.assertEqual(sum(groups.values()), self.fixture.entity_total)
        self.assertEqual(len(self.report.pois), self.fixture.poi_total)
        kinds = {record.display_name for record in self.report.pois}
        self.assertIn("床", kinds)
        self.assertIn("下界传送门", kinds)

    def test_structures(self) -> None:
        names = {item.structure for item in self.report.structures}
        self.assertIn("minecraft:village_plains", names)

    def test_player(self) -> None:
        self.assertEqual(len(self.report.players), 1)
        player = self.report.players[0]
        self.assertEqual(player.uuid, self.fixture.uuid)
        self.assertEqual(len(player.inventory), self.fixture.inventory_items)
        self.assertEqual(player.xp_level, 30)
        self.assertEqual(player.food_level, 17)
        self.assertEqual(player.play_time, 72000)
        self.assertEqual(player.deaths, 3)
        self.assertEqual(player.jump_count, 512)
        self.assertEqual(player.blocks_mined, 576)
        self.assertEqual(player.advancement_count, 2)
        helmet = next(item for item in player.inventory if "helmet" in item.item_id)
        self.assertEqual(helmet.enchantments, (("minecraft:protection", 4),))

    def test_advancement_timestamps(self) -> None:
        player = self.report.players[0]
        done = [item for item in player.advancements if item.done]
        self.assertEqual(len(done), 2)
        self.assertTrue(all(item.finished_at for item in done))

    def test_maps(self) -> None:
        self.assertEqual(len(self.report.maps), 1)
        map_info = self.report.maps[0]
        self.assertTrue(map_info.has_pixels)
        self.assertEqual(len(map_info.colors), 128 * 128)
        self.assertEqual(map_info.scale_label, "1:2")

    def test_dimension_info(self) -> None:
        overworld = self.report.dimension("minecraft:overworld")
        assert overworld is not None
        self.assertEqual(overworld.display_name, "主世界")
        self.assertEqual(overworld.region_files, 1)
        self.assertEqual(overworld.entity_count, self.fixture.entity_total)
        self.assertEqual(overworld.poi_count, self.fixture.poi_total)

    def test_timeline_and_activity(self) -> None:
        self.assertTrue(self.report.activity)
        self.assertTrue(self.report.timeline)
        self.assertTrue(any(event.kind == "advancement" for event in self.report.timeline))

    def test_timeline_orders_timed_before_untimed(self) -> None:
        """有时间的事件按时间倒序在前，"当前状态"类事件排在后面。"""
        kinds = [event.when for event in self.report.timeline]
        first_untimed = next((index for index, when in enumerate(kinds) if not when), len(kinds))
        self.assertTrue(all(when for when in kinds[:first_untimed]))
        timed = kinds[:first_untimed]
        self.assertEqual(timed, sorted(timed, reverse=True))

    def test_activity_spans_multiple_days(self) -> None:
        """两个区块的时间戳差 3 天：活跃记录必须按天分开。"""
        days = [day for day, count in self.report.activity]
        self.assertGreaterEqual(len(days), 2)
        self.assertGreaterEqual(days[-1] - days[0], 3 * 86400)

    def test_size_and_extras(self) -> None:
        self.assertGreater(self.report.size_bytes, 0)
        self.assertIn("会话锁", self.report.extras)
        self.assertIn("level.dat 备份", self.report.extras)

    def test_options_disable_chunks(self) -> None:
        report = scan_save(
            self.fixture.root,
            SaveScanOptions(read_chunks=False, read_entities=False, read_poi=False, read_maps=False),
        )
        self.assertEqual(report.total_chunks, 2)  # 只读偏移表也要能数出来
        self.assertEqual(report.chunks.top_blocks, ())
        self.assertEqual(report.entities, [])
        self.assertEqual(report.maps, [])


class LegacyScanTests(_TempCase):
    """旧版布局：region/ 在存档根、玩家在 playerdata/、字段内嵌在 level.dat。"""

    def setUp(self) -> None:
        super().setUp()
        self.fixture = build_legacy_save(self.tmp / "老存档")
        self.report = scan_save(self.fixture.root)

    def test_level_embedded_fields(self) -> None:
        level = self.report.level
        self.assertEqual(level.level_name, "老存档")
        self.assertEqual(level.difficulty_name, "困难")
        self.assertTrue(level.hardcore)
        self.assertFalse(level.allow_commands)
        self.assertEqual(level.seed, 42)
        assert level.spawn is not None
        self.assertEqual(level.spawn.block, (0, 64, 0))

    def test_legacy_paths(self) -> None:
        self.assertTrue(self.report.dimensions)
        self.assertEqual(self.report.total_chunks, 1)
        self.assertEqual(len(self.report.players), 1)

    def test_legacy_rules_and_border(self) -> None:
        names = {rule.short_name for rule in self.report.rules}
        self.assertIn("doFireTick", names)
        assert self.report.border is not None
        self.assertAlmostEqual(self.report.border.size, 1000.0)

    def test_legacy_chunk_without_ypos(self) -> None:
        # 老区块没有 yPos：解析器要自己推断，而且不能因此丢掉方块统计
        self.assertGreater(len(self.report.chunks.top_blocks), 0)


class EmptyRegionFileTests(_TempCase):
    """0 字节 ``.mca`` 是游戏为未探索区域预建的占位文件，不是损坏数据。

    真实存档里 ``poi/r.-1.-1.mca`` 就是 0 字节 —— 早期实现把它当"区域文件过短"
    抛错，导致整份存档点开就报错（实测两个真实存档都中招）。
    """

    def setUp(self) -> None:
        super().setUp()
        self.fixture = build_save(self.tmp / "world")
        # 两种"退化"区域文件：0 字节，以及只有半个头部的残缺文件
        poi_dir = self.fixture.root / "dimensions" / "minecraft" / "overworld" / "poi"
        (poi_dir / "r.5.5.mca").write_bytes(b"")
        entities_dir = self.fixture.root / "dimensions" / "minecraft" / "overworld" / "entities"
        (entities_dir / "r.5.5.mca").write_bytes(b"\x00\x00\x02\x00")

    def test_region_file_reports_empty(self) -> None:
        from offcraft.saves.anvil import RegionFile

        empty = RegionFile.open(
            self.fixture.root / "dimensions" / "minecraft" / "overworld" / "poi" / "r.5.5.mca"
        )
        self.assertTrue(empty.is_empty)
        # 头部要能读出来（全 0 = 没有区块），而不是抛错
        self.assertEqual(empty.chunk_coords(), [])

    def test_scan_survives_empty_region_files(self) -> None:
        report = scan_save(self.fixture.root)
        self.assertEqual(report.warnings, [])
        # 正常数据仍然读得到
        self.assertEqual(report.total_chunks, 2)
        self.assertEqual(len(report.pois), self.fixture.poi_total)
        self.assertEqual(sum(g.count for g in report.entities), self.fixture.entity_total)
        # 0 字节文件不计入"区域文件数"
        overworld = report.dimension("minecraft:overworld")
        assert overworld is not None
        self.assertEqual(overworld.region_files, 1)
        self.assertEqual(overworld.poi_chunk_files, 1)
        self.assertEqual(overworld.entities_chunk_files, 1)

    def test_count_only_ignores_empty_files(self) -> None:
        report = scan_save(
            self.fixture.root,
            SaveScanOptions(read_chunks=False, read_entities=False, read_poi=False, read_maps=False),
        )
        self.assertEqual(report.total_chunks, 2)
        overworld = report.dimension("minecraft:overworld")
        assert overworld is not None
        self.assertEqual(overworld.region_files, 1)


class ZeroUuidTests(unittest.TestCase):
    """全 0 UUID 是离线启动器写的占位符，不能当成真实 UUID 展示。"""

    def test_all_zero_rejected(self) -> None:
        self.assertEqual(_uuid_from_ints([0, 0, 0, 0]), "")

    def test_partial_zero_kept(self) -> None:
        # 真实存档里就有第一个 int 为 0 的合法 UUID（00000000-0000-300b-…）
        uid = _uuid_from_ints([0, 12299, -1676526043, -941056666])
        self.assertTrue(uid.startswith("00000000-0000-"))
        self.assertEqual(len(uid), 36)


class FullChunkCountingTests(_TempCase):
    """``full`` 只统计真正解析过的区块，且各维度独立计数。"""

    def setUp(self) -> None:
        super().setUp()
        self.fixture = build_save(self.tmp / "world")
        # 再塞一个只有半生成区块的维度，验证 per-dimension 计数
        from tests.save_fixture import _chunk_tree, _write_region

        nether = self.fixture.root / "dimensions" / "minecraft" / "the_nether" / "region"
        _write_region(
            nether / "r.0.0.mca",
            [
                (0, 0, _chunk_tree(cx=0, cz=0, status="minecraft:structure_starts", with_chest=False)),
                (1, 0, _chunk_tree(cx=1, cz=0, status="minecraft:full", with_chest=False)),
                (2, 0, _chunk_tree(cx=2, cz=0, status="minecraft:biomes", with_chest=False)),
            ],
        )

    def test_per_dimension_full_counts(self) -> None:
        report = scan_save(self.fixture.root)
        overworld = report.dimension("minecraft:overworld")
        nether = report.dimension("minecraft:the_nether")
        assert overworld is not None and nether is not None
        self.assertEqual(overworld.chunk_total, 2)
        self.assertEqual(overworld.chunk_full, 2)
        self.assertEqual(overworld.chunk_parsed, 2)
        self.assertEqual(nether.chunk_total, 3)
        self.assertEqual(nether.chunk_full, 1)  # 只有一个是 minecraft:full
        self.assertEqual(nether.chunk_parsed, 3)  # 但三个都解析了
        self.assertEqual(report.full_chunks, 3)

    def test_budget_marks_partial_parse(self) -> None:
        report = scan_save(self.fixture.root, SaveScanOptions(chunk_limit=1, read_entities=False, read_poi=False))
        # 预算用完时"总数"仍然准确（来自偏移表），"已解析"是 1
        self.assertEqual(report.total_chunks, 5)
        parsed = [dim.chunk_parsed for dim in report.dimensions if dim.chunk_total]
        self.assertEqual(sum(parsed), 1)
        self.assertIn("只解析了前 1 个", " ".join(report.warnings))


if __name__ == "__main__":
    unittest.main()
