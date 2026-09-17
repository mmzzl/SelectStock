"""tests/conftest.py — sys.path 注入 + 环境隔离。

- 注入 plugin/ 与仓库根目录（portfolio / stock_selector 可导入）
- 每个测试：PLUGIN_KLINE_DIR 指向空 tmp（不读真实 5186 只 parquet），
  PLUGIN_RULES_FILE 指向不存在路径（默认规则，测试可再 monkeypatch 覆盖）
"""
from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[3]

for _p in (str(_PLUGIN_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_KLINE_DIR", str(tmp_path / "kline"))
    monkeypatch.setenv("PLUGIN_RULES_FILE", str(tmp_path / "no_rules.json"))
