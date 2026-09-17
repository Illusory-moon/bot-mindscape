# -*- coding: utf-8 -*-
"""mindscape_config —— 共享配置读取（所有模块都从这里拿设置）

配置查找顺序：
  1. 环境变量 MINDSCAPE_CONFIG 指定的路径
  2. ~/.mindscape/config.yaml

读不到就返回空字典，各模块用自己的默认值兜底。
"""
import os

_CONFIG_CACHE = None


def config_path():
    return os.environ.get("MINDSCAPE_CONFIG") or os.path.expanduser("~/.mindscape/config.yaml")


def load(reload=False):
    """读配置（带缓存）。任何异常都返回 {}，绝不因此崩掉 bot。"""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not reload:
        return _CONFIG_CACHE
    path = config_path()
    data = {}
    if os.path.exists(path):
        try:
            import yaml  # type: ignore
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            # S03: 至少留一条线索，方便定位「配置为什么没生效」
            # 只记路径 + 异常类型，不输出可能含密钥的 YAML 内容
            try:
                import logging
                logging.getLogger("mindscape").warning(
                    "配置读取失败 path=%s type=%s", path, type(e).__name__)
            except Exception:
                pass
            data = {}
    _CONFIG_CACHE = data if isinstance(data, dict) else {}
    return _CONFIG_CACHE


def section(name, default=None):
    v = load().get(name)
    return v if isinstance(v, dict) else (default or {})


def bot_entries():
    """返回 memory 段里配置的 bot 列表。"""
    mem = section("memory")
    bots = mem.get("bots")
    return bots if isinstance(bots, list) else []