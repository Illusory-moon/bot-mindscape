# ── bot-mindscape: 唤醒判定（插在 `if not is_wake:` 之前）──
import random as _ms_random, time as _ms_time
_ms_sid = str(event.get_self_id())
_ms_pb = (self.auto_wake_cfg.get("per_bot") or {}).get(_ms_sid) or {}
_ms_names = ((self.auto_wake_cfg.get("per_bot_names") or {}).get(_ms_sid)
             or self.auto_wake_cfg.get("names", []))
_ms_excl = self.auto_wake_cfg.get("exclude_names", [])
_ms_groups = [str(g) for g in
              ((self.auto_wake_cfg.get("restricted_groups") or {}).get(_ms_sid) or [])]
_ms_text = event.message_str or ""
_ms_enabled = bool(_ms_pb.get("enabled", True))
_ms_group_ok = (not _ms_groups) or (str(event.get_group_id()) in _ms_groups)
_ms_mentioned = (_ms_group_ok
                 and any(_n in _ms_text for _n in _ms_names)
                 and not any(_e in _ms_text for _e in _ms_excl))
_ms_prob = float(_ms_pb.get("sample_prob")
                 if _ms_pb.get("sample_prob") is not None
                 else self.auto_wake_cfg.get("sample_prob", 0.02))
_ms_minint = float(_ms_pb.get("min_interval")
                   if _ms_pb.get("min_interval") is not None
                   else self.auto_wake_cfg.get("min_interval", 600))
_ms_now = _ms_time.time()
_ms_ok_interval = (_ms_now - self._last_auto_wake_map.get(_ms_sid, 0.0)) >= _ms_minint
_ms_sampled = _ms_random.random() < _ms_prob
if (_ms_enabled and _ms_group_ok and not event.is_at_or_wake_command
        and (_ms_mentioned or (_ms_sampled and _ms_ok_interval))):
    self._last_auto_wake_map[_ms_sid] = _ms_now
    event.is_wake = True
    event.is_at_or_wake_command = True
    try:
        logger.info("[auto_wake:%s] self=%s group=%s"
                    % ("mention" if _ms_mentioned else "sample",
                       _ms_sid, event.get_group_id()))
    except Exception:
        pass
