# -*- coding: utf-8 -*-
"""mindscape_config_gui —— 图形化配置器

给不想改源码的人用：填表 → 保存 config.yaml。

依赖：只用 Python 标准库（tkinter），不需要 pip install 任何东西。
用法：python scripts/config_gui.py
"""
import copy
import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import yaml
except ImportError:
    print("需要 PyYAML：pip install pyyaml")
    sys.exit(1)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(ROOT, "config", "config.yaml")
EXAMPLE = os.path.join(ROOT, "config", "config.example.yaml")


def load_yaml(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def dump_yaml(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


class Field:
    """一个表单字段：标签 + 输入框。"""

    def __init__(self, parent, row, label, value="", width=52, browse=None):
        self.label = label
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(10, 6), pady=4)
        self.var = tk.StringVar(value=str(value if value is not None else ""))
        self.entry = ttk.Entry(parent, textvariable=self.var, width=width)
        self.entry.grid(row=row, column=1, sticky="we", pady=4)
        if browse:
            ttk.Button(parent, text="浏览", width=6,
                       command=lambda: self._browse(browse)).grid(row=row, column=2, padx=(4, 10))

    def _browse(self, kind):
        p = filedialog.askopenfilename() if kind == "file" else filedialog.askdirectory()
        if p:
            self.var.set(p)

    def get(self):
        return self.var.get().strip()

    def set(self, v):
        self.var.set(str(v if v is not None else ""))


class App:
    def __init__(self, root):
        self.root = root
        root.title("bot-mindscape 配置器")
        root.geometry("720x560")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        self._original = {}      # R08: 保留加载到的原始配置，保存时只改动编辑过的键
        self._path = None        # R08: 记住配置来源路径
        self.nb = ttk.Notebook(root)
        self.nb.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 0))
        self.fields = {}

        self._tab_cognition()
        self._tab_expression()
        self._tab_immersion()
        self._tab_ops()
        self._tab_waking()

        bar = ttk.Frame(root)
        bar.grid(row=1, column=0, sticky="we", padx=8, pady=8)
        ttk.Button(bar, text="从示例载入", command=self.load_example).pack(side="left", padx=3)
        ttk.Button(bar, text="加载现有配置", command=self.load_config).pack(side="left", padx=3)
        ttk.Button(bar, text="保存配置", command=self.save_config).pack(side="right", padx=3)

        self.status = ttk.Label(root, text="就绪", foreground="#666")
        self.status.grid(row=2, column=0, sticky="w", padx=12, pady=(0, 8))

        self.load_example()

    # ── 各层表单 ──
    def _page(self, title):
        f = ttk.Frame(self.nb)
        f.columnconfigure(1, weight=1)
        self.nb.add(f, text=title)
        return f

    def _add(self, page, key, row, label, value="", browse=None):
        self.fields[key] = Field(page, row, label, value, browse=browse)

    def _tab_cognition(self):
        p = self._page("① 认知层")
        ttk.Label(p, text="长期记忆：bot 记得住事，且不会撑爆请求",
                  foreground="#888").grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(8, 4))
        self._add(p, "bot_id", 1, "Bot 账号 ID", "20000000")
        self._add(p, "bot_name", 2, "Bot 名称", "bot-name")
        self._add(p, "diary_path", 3, "记忆文件", "./data/bot-name.md")
        self._add(p, "memory_chars", 4, "每轮注入字数", "2500")
        self._add(p, "src_db", 5, "消息库路径", "./data/messages.db", browse="file")
        self._add(p, "src_table", 6, "消息表名", "messages")
        self._add(p, "src_where", 7, "过滤条件（可空）", "event_name='group_message'")
        self._add(p, "groups", 8, "只记录的群（逗号分隔）", "20000001")
        self._add(p, "llm_base", 9, "LLM 接口地址", "https://api.example.com/v1")
        self._add(p, "llm_key_env", 10, "API Key 环境变量名", "MINDSCAPE_API_KEY")
        self._add(p, "llm_model", 11, "LLM 模型名", "your-model")

    def _tab_expression(self):
        p = self._page("② 表达层")
        ttk.Label(p, text="表情包：自动收集契合人设的图，并主动使用",
                  foreground="#888").grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(8, 4))
        self._add(p, "sticker_dir", 1, "图库目录", "./data/stickers")
        self._add(p, "sticker_cat", 2, "图库分类（本 bot 独占）", "bot-name")
        self._add(p, "sample_prob", 3, "采样概率 (0~1)", "0.10")
        self._add(p, "vmodel", 4, "视觉模型名", "your-vision-model")
        self._add(p, "persona", 5, "人设简述（用于判断契合度）", "一名温柔的学生少女")
        self._add(p, "force_prob", 6, "强制配图概率 (0~1)", "0.10")
        self._add(p, "candidates", 7, "选图候选数", "12")

    def _tab_immersion(self):
        p = self._page("③ 沉浸层")
        ttk.Label(p, text="报错拦截 + 输出规范：绝不出戏",
                  foreground="#888").grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(8, 4))
        self._add(p, "guard_patterns", 1, "拦截关键词（一行一个）", "API Error\nRequest timed out", )
        self.fields["guard_patterns"].entry.grid_forget()
        txt = tk.Text(p, height=5, width=52)
        txt.grid(row=1, column=1, sticky="we", pady=4)
        self.guard_text = txt
        self._add(p, "join_with", 2, "段落连接符", "，")
        self._add(p, "fmt_targets", 3, "规范输出的 bot（逗号分隔）", "20000000")

    def _tab_ops(self):
        p = self._page("④ 运维层")
        ttk.Label(p, text="会话防膨胀：定期清理，防止请求超时",
                  foreground="#888").grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(8, 4))
        self._add(p, "jan_db", 1, "会话库路径", "./data/conversations.db", browse="file")
        self._add(p, "jan_table", 2, "会话表名", "conversations")
        self._add(p, "jan_column", 3, "内容字段名", "content")
        self._add(p, "jan_max_mb", 4, "单条上限 (MB)", "2.0")

    def _tab_waking(self):
        p = self._page("⑤ 唤醒层")
        ttk.Label(p, text="什么时候该回应：@必回 / 提到名字必回 / 低概率冒泡",
                  foreground="#888").grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(8, 4))
        self._add(p, "wake_names", 1, "触发名字（一行一个）", "bot-name\n小名")
        self.fields["wake_names"].entry.grid_forget()
        names_txt = tk.Text(p, height=4, width=52)
        names_txt.grid(row=1, column=1, sticky="we", pady=4)
        self.wake_names_text = names_txt
        self._add(p, "wake_excl", 2, "排除词（一行一个）", "")
        self.fields["wake_excl"].entry.grid_forget()
        excl_txt = tk.Text(p, height=3, width=52)
        excl_txt.grid(row=2, column=1, sticky="we", pady=4)
        self.wake_excl_text = excl_txt
        self._add(p, "wake_sample", 3, "冒泡概率 (0~1)", "0.02")
        self._add(p, "wake_interval", 4, "冒泡最小间隔（秒）", "600")
        self._add(p, "wake_groups", 5, "只在这些群活动（逗号分隔）", "20000001")
        ttk.Label(p, text="⚠️ 唤醒判定在框架层，插件无法介入；",
                  foreground="#a06000").grid(row=6, column=0, columnspan=3, sticky="w", padx=10, pady=(10, 0))
        ttk.Label(p, text="   本仓库 patches/ 目录提供自动安装脚本。",
                  foreground="#a06000").grid(row=7, column=0, columnspan=3, sticky="w", padx=10)

    # ── 载入 / 保存 ──
    def _get(self, key, default=""):
        f = self.fields.get(key)
        return f.get() if f else default

    def load_example(self):
        data = load_yaml(EXAMPLE)
        self._original = copy.deepcopy(data)
        self._path = None
        self._fill(data)
        self.status.config(text="已载入示例配置")

    def load_config(self):
        path = DEFAULT_CONFIG if os.path.exists(DEFAULT_CONFIG) else filedialog.askopenfilename()
        if not path or not os.path.exists(path):
            return
        data = load_yaml(path)
        self._original = copy.deepcopy(data)
        self._path = path
        self._fill(data)
        self.status.config(text="已载入 " + path)

    def _fill(self, d):
        mem = (d.get("memory") or {})
        bots = mem.get("bots") or [{}]
        b0 = bots[0] if bots else {}
        self._get_set("bot_id", b0.get("self_id"))
        self._get_set("bot_name", b0.get("name"))
        self._get_set("diary_path", b0.get("diary"))
        self._get_set("memory_chars", b0.get("memory_chars") or mem.get("max_chars"))

        src = (d.get("diary") or {}).get("source") or {}
        self._get_set("src_db", src.get("db"))
        self._get_set("src_table", src.get("table"))
        self._get_set("src_where", src.get("where"))
        tg = ((d.get("diary") or {}).get("targets") or [{}])[0]
        self._get_set("groups", ",".join(str(x) for x in (tg.get("groups") or [])))
        llm = (d.get("diary") or {}).get("llm") or {}
        self._get_set("llm_base", llm.get("api_base"))
        self._get_set("llm_key_env", llm.get("api_key_env"))
        self._get_set("llm_model", llm.get("model"))

        st = d.get("stickers") or {}
        self._get_set("sticker_dir", st.get("dir"))
        stg = (st.get("targets") or [{}])[0]
        self._get_set("sticker_cat", stg.get("category"))
        self._get_set("sample_prob", st.get("sample_prob"))
        j = st.get("judge") or {}
        self._get_set("vmodel", j.get("model"))
        self._get_set("persona", j.get("persona"))
        snd = st.get("send") or {}
        self._get_set("force_prob", snd.get("force_prob"))
        self._get_set("candidates", snd.get("candidates"))

        g = d.get("guard") or {}
        self.guard_text.delete("1.0", "end")
        for p_ in (g.get("patterns") or []):
            self.guard_text.insert("end", str(p_) + "\n")
        f = d.get("format") or {}
        self._get_set("join_with", f.get("join_with"))
        self._get_set("fmt_targets", ",".join(str(x) for x in (f.get("targets") or [])))

        w = d.get("waking") or {}
        wb = (w.get("bots") or [{}])[0]
        self.wake_names_text.delete("1.0", "end")
        for n_ in (wb.get("names") or []):
            self.wake_names_text.insert("end", str(n_) + "\n")
        self.wake_excl_text.delete("1.0", "end")
        for e_ in (wb.get("exclude") or []):
            self.wake_excl_text.insert("end", str(e_) + "\n")
        sp = wb.get("sample_prob")
        self._get_set("wake_sample", sp if sp is not None else w.get("sample_prob"))
        self._get_set("wake_interval", wb.get("min_interval") or w.get("min_interval"))
        self._get_set("wake_groups", ",".join(str(x) for x in (wb.get("groups") or [])))

        jn = d.get("janitor") or {}
        self._get_set("jan_db", jn.get("db"))
        self._get_set("jan_table", jn.get("table"))
        self._get_set("jan_column", jn.get("column"))
        self._get_set("jan_max_mb", jn.get("max_mb"))

    def _get_set(self, key, v):
        if key in self.fields:
            self.fields[key].set(v if v is not None else "")

    def save_config(self):
        sid = self._get("bot_id", "20000000")
        name = self._get("bot_name", "bot-name")
        diary = self._get("diary_path", "./data/bot-name.md")
        groups = [x.strip() for x in self._get("groups").replace("，", ",").split(",") if x.strip()]
        fmt_t = [x.strip() for x in self._get("fmt_targets").replace("，", ",").split(",") if x.strip()]

        def fnum(key, dv):
            try:
                return float(self._get(key) or dv)
            except ValueError:
                return dv

        # R08: 从原始配置深拷贝起步，只覆盖界面真正编辑的键，
        # 这样多 bot、自定义字段、未展示的配置都不会被抹掉。
        data = copy.deepcopy(self._original) if self._original else {}
        data.setdefault("memory", {})
        data.setdefault("diary", {})
        data.setdefault("stickers", {})
        data.setdefault("guard", {})
        data.setdefault("format", {})
        data.setdefault("janitor", {})
        data.setdefault("waking", {})
        patch = {
            "memory": {
                "max_chars": int(fnum("memory_chars", 2500)),
                "bots": [{
                    "self_id": sid, "name": name, "diary": diary,
                    "memory_chars": int(fnum("memory_chars", 2500)),
                }],
            },
            "diary": {
                "source": {
                    "db": self._get("src_db"), "table": self._get("src_table") or "messages",
                    "where": self._get("src_where"),
                    "fields": {"time": "timestamp", "seq": "sequence", "data": "data"},
                },
                "targets": [{
                    "self_id": sid, "name": name, "groups": groups,
                    "output": diary, "state": diary + ".state.json",
                }],
                "llm": {
                    "api_base": self._get("llm_base"),
                    "api_key_env": self._get("llm_key_env") or "MINDSCAPE_API_KEY",
                    "model": self._get("llm_model"),
                },
                "batch": 40, "max_input_chars": 14000, "max_tokens": 900,
            },
            "stickers": {
                "dir": self._get("sticker_dir") or "./data/stickers",
                "sample_prob": fnum("sample_prob", 0.10),
                "targets": [{"self_id": sid, "category": self._get("sticker_cat") or name}],
                "judge": {
                    "api_base": self._get("llm_base"),
                    "api_key_env": self._get("llm_key_env") or "MINDSCAPE_API_KEY",
                    "model": self._get("vmodel"),
                    "persona": self._get("persona"),
                    "max_tokens": 2000,
                },
                "send": {"force_prob": fnum("force_prob", 0.10),
                         "candidates": int(fnum("candidates", 12))},
            },
            "guard": {
                "patterns": [l.strip() for l in self.guard_text.get("1.0", "end").splitlines() if l.strip()],
            },
            "format": {"targets": fmt_t, "join_with": self._get("join_with") or "，"},
            "waking": {
                "sample_prob": fnum("wake_sample", 0.02),
                "min_interval": int(fnum("wake_interval", 600)),
                "bots": [{
                    "self_id": sid,
                    "enabled": True,
                    "names": [l.strip() for l in self.wake_names_text.get("1.0", "end").splitlines() if l.strip()],
                    "exclude": [l.strip() for l in self.wake_excl_text.get("1.0", "end").splitlines() if l.strip()],
                    "reply_on_at": True,
                    "sample_prob": fnum("wake_sample", 0.02),
                    "min_interval": int(fnum("wake_interval", 600)),
                    "groups": groups,
                }],
            },
            "janitor": {
                "db": self._get("jan_db"), "table": self._get("jan_table") or "conversations",
                "column": self._get("jan_column") or "content", "max_mb": fnum("jan_max_mb", 2.0),
            },
        }
        # 浅层合并：保留原配置里本次未编辑的键
        for sec, val in patch.items():
            if sec in ("memory", "stickers", "waking", "guard", "format", "janitor", "diary"):
                if isinstance(data.get(sec), dict) and isinstance(val, dict):
                    data[sec].update(val)
                else:
                    data[sec] = val
            else:
                data[sec] = val

        target = self._path or DEFAULT_CONFIG
        try:
            dump_yaml(target, data)
            self.status.config(text="已保存：" + target)
            messagebox.showinfo("保存成功", "配置已写入：\n" + target)
        except Exception as e:
            messagebox.showerror("保存失败", str(e)[:300])


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()