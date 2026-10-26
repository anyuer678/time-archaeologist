"""分类启发式测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from time_arch.classify import classify_file


class TestClassify(unittest.TestCase):
    def test_source(self):
        self.assertEqual(classify_file("src/main.py"), "source")
        self.assertEqual(classify_file(r"app\index.html"), "source")
        self.assertEqual(classify_file("a/b/c/tool.go"), "source")

    def test_build(self):
        self.assertEqual(classify_file("build/x.exe"), "build")
        self.assertEqual(classify_file("__pycache__/m.cpython-312.pyc"), "build")
        self.assertEqual(classify_file("node_modules/pkg/index.js"), "build")

    def test_test(self):
        self.assertEqual(classify_file("tests/test_parser.py"), "test")
        self.assertEqual(classify_file("spec/service.spec.ts"), "test")

    def test_doc_log_config_data(self):
        self.assertEqual(classify_file("README.md"), "doc")
        self.assertEqual(classify_file("docs/guide.rst"), "doc")
        self.assertEqual(classify_file("logs/app.log"), "log")
        self.assertEqual(classify_file("run.log"), "log")
        self.assertEqual(classify_file("settings.json"), "config")
        self.assertEqual(classify_file("data/metrics.csv"), "data")
        self.assertEqual(classify_file("backup.zip"), "data")

    def test_other(self):
        self.assertEqual(classify_file("unknown.bin"), "other")

    def test_windows_sep(self):
        self.assertEqual(classify_file(r"src\main.py"), "source")


if __name__ == "__main__":
    unittest.main()
