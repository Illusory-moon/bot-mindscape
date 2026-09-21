# -*- coding: utf-8 -*-
"""mindscape_config —— 共享配置读取（所有模块都从这里拿设置）

配置查找顺序：
  1. 环境变量 MINDSCAPE_CONFIG 指定的路径
  2. 插件数据目录 data/plugin_data/astrbot_plugin_mindscape/config.yaml
     （在 AstrBot 里运行时；命令行脚本没有框架，回退到仓库的 config/ 目录）

**配置文件所在的目录就是基准目录** —— 配置里写的 ./data/xxx.md 都相对它解析，
所以数据跟着配置一起走，整体搬迁不会断。

读不到就返回空字典，各模块用自己的默认值兜底。
"""
import os

try:                                   # 插件内：日志必须从框架走（插件市场规范）
    from astrbot.api import logger
except Exception:                      # 命令行脚本：没有框架，跳过日志
    logger = None

_CONFIG_CACHE = None

PLUGIN_NAME = "astrbot_plugin_mindscape"


def data_dir():
    """插件数据目录（插件市场的规范位置）。

    在 AstrBot 里运行时用 StarTools 拿 data/plugin_data/<插件名>/；
    命令行脚本里没有框架，回退到仓库的 config/ 目录。
    """
    try:
        from astrbot.api.star import StarTools
        return str(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(here, "config")


def config_path():
    return os.environ.get("MINDSCAPE_CONFIG") or os.path.join(data_dir(), "config.yaml")


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
            if logger is not None:
                logger.warning("配置读取失败 path=%s type=%s", path, type(e).__name__)
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
