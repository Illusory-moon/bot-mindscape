# ══════════════════════════════════════════════════════════════
# bot-mindscape · auto-wake 配置（由 install.py 插入到 __init__ 内）
# 支持：YAML 主配置 + 旧版 JSON 覆盖；每 bot 独立名字/概率/间隔/群白名单
# ══════════════════════════════════════════════════════════════
import json as _ms_json, os as _ms_os
self.auto_wake_cfg = {
    "names": [],
    "exclude_names": [],
    "sample_prob": 0.02,
    "min_interval": 600,
    "per_bot": {},
    "per_bot_names": {},
    "restricted_groups": {},
}
# 1) 主配置：~/.mindscape/config.yaml（或 $MINDSCAPE_CONFIG 指定）
try:
    _ms_cp = _ms_os.environ.get("MINDSCAPE_CONFIG") or _ms_os.path.expanduser("~/.mindscape/config.yaml")
    if _ms_os.path.exists(_ms_cp):
        import yaml as _ms_yaml
        with open(_ms_cp, encoding="utf-8") as _ms_f:
            _ms_doc = _ms_yaml.safe_load(_ms_f) or {}
        _ms_w = _ms_doc.get("waking") or {}
        if isinstance(_ms_w, dict):
            if _ms_w.get("sample_prob") is not None:
                self.auto_wake_cfg["sample_prob"] = _ms_w["sample_prob"]
            if _ms_w.get("min_interval") is not None:
                self.auto_wake_cfg["min_interval"] = _ms_w["min_interval"]
            for _ms_b in (_ms_w.get("bots") or []):
                if not isinstance(_ms_b, dict):
                    continue
                _ms_sid = str(_ms_b.get("self_id") or "")
                if not _ms_sid:
                    continue
                if _ms_b.get("names"):
                    self.auto_wake_cfg["per_bot_names"][_ms_sid] = list(_ms_b["names"])
                for _ms_e in (_ms_b.get("exclude") or []):
                    if _ms_e not in self.auto_wake_cfg["exclude_names"]:
                        self.auto_wake_cfg["exclude_names"].append(_ms_e)
                self.auto_wake_cfg["per_bot"][_ms_sid] = {
                    "enabled": bool(_ms_b.get("enabled", True)),
                    "sample_prob": _ms_b.get("sample_prob"),
                    "min_interval": _ms_b.get("min_interval"),
                }
                if _ms_b.get("groups"):
                    self.auto_wake_cfg["restricted_groups"][_ms_sid] = [str(x) for x in _ms_b["groups"]]
except Exception:
    pass
# 2) 兼容旧版 JSON（存在则覆盖，便于从旧部署平滑迁移）
try:
    _ms_jp = "/opt/astrbot/data/auto_wake_cfg.json"
    if _ms_os.path.exists(_ms_jp):
        with open(_ms_jp, encoding="utf-8") as _ms_f:
            _ms_c = _ms_json.load(_ms_f)
        if isinstance(_ms_c, dict):
            self.auto_wake_cfg.update(_ms_c)
except Exception:
    pass
self._last_auto_wake_map = {}
