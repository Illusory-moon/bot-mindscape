#!/bin/bash
# mindscape_cleanup.sh —— 定期清掉「占地方但没用」的东西（两个 bot 共用一份）
#
# 为什么要有它（2026-10-31 体检结论）：
#   · /opt/astrbot/data/temp   —— 图片/语音的本地副本，**没有清理机制**，两个月攒到 620 MB ✗
#   · /opt/astrbot/data/*.bak-* —— 每次改配置/人格都会留一份 DB 备份，堆在 data 根 406 MB ✗
#   · voice_cache              —— TTS 缓存，只增不减 ✗
#
# 三条纪律：
#   1. 只清「肯定没用的」：temp 只删**超过 N 天**的；备份只删**超出保留数**的；缓存超上限才删最旧 ✗
#   2. **绝不碰**：data_v4.db 本体 / stickers / plugins / persona-*.md / *.md 知识文件
#   3. 每次输出一行摘要，方便对账 ✓
#
# 用法：bash mindscape_cleanup.sh [--dry-run]
# 可调：MS_DATA / TEMP_KEEP_DAYS / BAK_KEEP / VOICE_MAX_MB

set -u

DATA=${MS_DATA:-/opt/astrbot/data}
TEMP_KEEP_DAYS=${TEMP_KEEP_DAYS:-30}
BAK_KEEP=${BAK_KEEP:-5}
VOICE_MAX_MB=${VOICE_MAX_MB:-300}
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

say() { echo "[cleanup] $*"; }
maybe_del() {  # maybe_del <文件>
  if [ "$DRY" = "1" ]; then say "  (dry-run) 会删 $1"; else rm -f -- "$1"; fi
}

# ── ① temp：删超过 N 天没动过的 ────────────────────────────────
T="$DATA/temp"
if [ -d "$T" ]; then
  BEFORE=$(du -sm "$T" 2>/dev/null | cut -f1)
  N=$(find "$T" -type f -mtime +"$TEMP_KEEP_DAYS" | wc -l)
  if [ "$DRY" = "1" ]; then
    say "temp: 有 $N 个文件超过 ${TEMP_KEEP_DAYS} 天（dry-run 不删）"
  else
    find "$T" -type f -mtime +"$TEMP_KEEP_DAYS" -delete 2>/dev/null
  fi
  AFTER=$(du -sm "$T" 2>/dev/null | cut -f1)
  say "temp: 删了 $N 个（>${TEMP_KEEP_DAYS}天）｜ ${BEFORE}MB → ${AFTER}MB"
fi

# ── ② DB 备份：data/backups 里只留最近 N 份 ──────────────────
B="$DATA/backups"
if [ -d "$B" ]; then
  # 只轮转我们自己产生的那几类，别的文件一律不动
  ls -1t "$B"/data_v4.db.bak-* "$B"/data_v4.db.snapshot-* 2>/dev/null \
    | tail -n +$((BAK_KEEP + 1)) | while read -r f; do
        maybe_del "$f"
      done
  say "backups: 保留最近 ${BAK_KEEP} 份｜当前 $(du -sh "$B" 2>/dev/null | cut -f1)"
fi

# ── ③ voice_cache：超上限就删最旧的 ──────────────────────────
V="$DATA/voice_cache"
if [ -d "$V" ]; then
  VM=$(du -sm "$V" 2>/dev/null | cut -f1)
  if [ "${VM:-0}" -gt "$VOICE_MAX_MB" ]; then
    ls -1t "$V" 2>/dev/null | tail -n +$(( $(ls -1 "$V" 2>/dev/null | wc -l) / 2 + 1 )) \
      | while read -r f; do maybe_del "$V/$f"; done
    say "voice_cache: 超过 ${VOICE_MAX_MB}MB，删掉旧的一半｜ $VM MB → $(du -sm "$V" 2>/dev/null | cut -f1) MB"
  else
    say "voice_cache: ${VM:-0}MB，未到上限 ${VOICE_MAX_MB}MB，不动 ✓"
  fi
fi

say "磁盘：$(df -h "$DATA" 2>/dev/null | awk 'NR==2{print $3 " / " $2 " (" $5 ")"}')"
