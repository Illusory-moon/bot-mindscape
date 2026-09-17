# -*- coding: utf-8 -*-
"""把项目核心代码打包成单个 Markdown，便于贴给外部审查模型。"""
import io, os, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "docs", "review-bundle.md")

# 审查需要的核心文件（跳过自检脚本、补丁块、示例数据）
FILES = [
    "README.md",
    "config/config.example.yaml",
    "plugins/mindscape_config.py",
    "plugins/mindscape_guard.py",
    "plugins/mindscape_memory.py",
    "plugins/mindscape_recall.py",
    "plugins/mindscape_diary.py",
    "plugins/mindscape_stickers.py",
    "plugins/mindscape_sticker_use.py",
    "plugins/mindscape_format.py",
    "plugins/mindscape_janitor.py",
    "scripts/web_ui.py",
    "scripts/import_stickers.py",
    "patches/astrbot/install.py",
]

LANG = {".py": "python", ".yaml": "yaml", ".md": "markdown"}


def main():
    parts = []
    prompt = os.path.join(HERE, "docs", "review-prompt.md")
    if os.path.exists(prompt):
        parts.append(open(prompt, encoding="utf-8").read())
        parts.append("\n\n---\n\n# 附：完整代码\n")
    total = 0
    for rel in FILES:
        p = os.path.join(HERE, rel.replace("/", os.sep))
        if not os.path.exists(p):
            print("  [跳过] " + rel)
            continue
        txt = open(p, encoding="utf-8").read()
        total += len(txt)
        ext = os.path.splitext(p)[1]
        parts.append("\n## %s\n\n```%s\n%s\n```\n" % (rel, LANG.get(ext, ""), txt))
        print("  [加入] %-42s %6d 字符" % (rel, len(txt)))
    body = "".join(parts)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(body)
    print()
    print("总字符数: %d（约 %d k tokens）" % (len(body), len(body) // 1500))
    print("输出: " + OUT)


if __name__ == "__main__":
    main()