"""通用工具测试：fmt_size / sanitize_text / extended_path / ts_to_iso / fmt_time_local。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from time_arch.util import (
    extended_path,
    fmt_size,
    fmt_time_local,
    sanitize_text,
    ts_to_iso,
)


class TestFmtSize(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(fmt_size(0), "0 B")
        self.assertEqual(fmt_size(1023), "1023 B")

    def test_units(self):
        self.assertEqual(fmt_size(1024), "1.0 KB")
        self.assertEqual(fmt_size(1536), "1.5 KB")
        self.assertEqual(fmt_size(1024 ** 2), "1.0 MB")

    def test_tb_cap(self):
        # 超过 TB 后封顶，不再继续缩放
        self.assertEqual(fmt_size(1024 ** 5), "1024.0 TB")

    def test_invalid_returns_dash(self):
        self.assertEqual(fmt_size(None), "-")
        self.assertEqual(fmt_size("abc"), "-")
        self.assertEqual(fmt_size(""), "-")

    def test_float_bytes(self):
        self.assertEqual(fmt_size(512.6), "513 B")


class TestSanitizeText(unittest.TestCase):
    def test_normal_text_unchanged(self):
        self.assertEqual(sanitize_text("普通中文 text 123"), "普通中文 text 123")

    def test_lone_surrogate_replaced(self):
        # 单独代理项无法编码 UTF-8，应被替换而不是抛异常（防个别异常文件名拖垮整批写入）
        dirty = "ok\udcffend"
        cleaned = sanitize_text(dirty)
        cleaned.encode("utf-8")  # 不应抛 UnicodeEncodeError
        self.assertNotEqual(cleaned, dirty)


class TestExtendedPath(unittest.TestCase):
    def test_drive_path_prefixed(self):
        p = "C:\\proj\\a.txt"
        self.assertEqual(extended_path(p), "\\\\?\\" + p)

    def test_already_prefixed_unchanged(self):
        p = "\\\\?\\C:\\proj\\a.txt"
        self.assertEqual(extended_path(p), p)

    def test_unc_path_not_prefixed(self):
        p = "\\\\server\\share\\a.txt"
        self.assertEqual(extended_path(p), p)

    def test_relative_path_not_prefixed(self):
        self.assertEqual(extended_path("rel\\a.txt"), "rel\\a.txt")


class TestTsToIso(unittest.TestCase):
    def test_epoch(self):
        self.assertEqual(ts_to_iso(0), "1970-01-01T00:00:00.000+00:00")

    def test_known_value(self):
        # 2026-01-01 00:00:00 UTC = 1767225600
        self.assertEqual(ts_to_iso(1767225600.0), "2026-01-01T00:00:00.000+00:00")

    def test_overflow_returns_empty(self):
        self.assertEqual(ts_to_iso(1e20), "")


class TestFmtTimeLocal(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertEqual(fmt_time_local(None), "-")
        self.assertEqual(fmt_time_local(""), "-")

    def test_invalid_iso_returns_input(self):
        self.assertEqual(fmt_time_local("not-a-time"), "not-a-time")

    def test_valid_iso_local_format(self):
        import re
        out = fmt_time_local("2026-01-01T12:00:00+00:00")
        self.assertRegex(out, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


if __name__ == "__main__":
    unittest.main()
