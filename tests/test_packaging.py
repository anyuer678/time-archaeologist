"""打包元数据一致性：pyproject 与包版本、入口目标保持一致。"""
from __future__ import annotations

import importlib
import tomllib
import unittest
from pathlib import Path

from time_arch import __version__

REPO = Path(__file__).resolve().parents[1]


class PackagingTest(unittest.TestCase):
    def setUp(self):
        self.data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))

    def test_metadata_and_dynamic_version(self):
        project = self.data["project"]
        self.assertEqual(project["name"], "time-archaeologist")
        self.assertIn(">=3.10", project["requires-python"])
        self.assertEqual(project["license"], {"text": "MIT"})
        attr = self.data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
        mod_name, name = attr.rsplit(".", 1)
        self.assertEqual(getattr(importlib.import_module(mod_name), name), __version__)
        self.assertEqual(self.data["tool"]["setuptools"]["packages"],
                         ["time_arch", "time_arch.ui"])

    def test_entry_points_importable(self):
        project = self.data["project"]
        scripts = {**project["scripts"], **project["gui-scripts"]}
        self.assertEqual(scripts["time-archaeologist"], "time_arch.cli:main")
        self.assertEqual(scripts["time-archaeologist-gui"], "time_arch.ui.app:main")
        for target in scripts.values():
            mod_name, func_name = target.split(":")
            self.assertTrue(callable(getattr(importlib.import_module(mod_name), func_name)))


if __name__ == "__main__":
    unittest.main()
