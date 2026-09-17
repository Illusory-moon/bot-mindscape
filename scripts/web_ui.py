# -*- coding: utf-8 -*-
"""mindscape_web —— 可选的本地 Web 管理界面

依赖：PyYAML + Python 标准库 http.server。
默认只监听 127.0.0.1；若改 --host 监听外部地址，请自行加认证（本服务只有页面令牌，不是完整鉴权）。

功能：
  1. 查看 / 编辑配置（YAML 原文，保存前会自动校验语法）
  2. 预览图库（按分类分组的图片墙）
  3. 查看记忆文件（日记 / 人物画像）

用法：
    python scripts/web_ui.py            # 默认 http://127.0.0.1:8777
    python scripts/web_ui.py --port 9000
"""
import argparse
import html
import json
import os
import sys
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

# R01: 写操作需要一次性令牌；仅允许本机来源
TOKEN = secrets.token_urlsafe(16)
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_BODY = 512 * 1024          # 配置体上限 512KB

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "plugins"))

try:
    import mindscape_config as cfg
except Exception:
    cfg = None

try:
    import yaml
except ImportError:
    yaml = None


def _abs(path):
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    if cfg:
        return os.path.join(os.path.dirname(cfg.config_path()), path)
    return os.path.join(HERE, path)


def config_path():
    if cfg:
        return cfg.config_path()
    return os.path.join(HERE, "config", "config.yaml")


def stickers_dir():
    s = (cfg.section("stickers") if cfg else {}) or {}
    return _abs(s.get("dir") or "./data/stickers")


def index_path():
    s = (cfg.section("stickers") if cfg else {}) or {}
    d = stickers_dir()
    return _abs(s.get("index") or os.path.join(d, "index.json"))


def read_text(path, limit=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            t = f.read()
        return t[:limit] if limit else t
    except Exception as e:
        return "(读取失败: %s)" % str(e)[:100]


PAGE = """<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">
<title>bot-mindscape 管理台</title><style>
body{{background:#14121a;color:#eee;font-family:system-ui,sans-serif;margin:0;padding:20px}}
h1{{font-size:18px;color:#e85a9b;margin:0 0 12px}}
.tabs{{margin-bottom:14px}}.tabs button{{background:#241f30;color:#bbb;border:1px solid #332c42;
padding:6px 14px;margin-right:6px;border-radius:6px;cursor:pointer}}
.tabs button.on{{background:#e85a9b;color:#fff;border-color:#e85a9b}}
.panel{{display:none}}.panel.on{{display:block}}
textarea{{width:100%;height:60vh;background:#1b1823;color:#ddd;border:1px solid #332c42;
border-radius:8px;padding:12px;font-family:monospace;font-size:13px;box-sizing:border-box}}
.btn{{background:#e85a9b;color:#fff;border:0;padding:8px 18px;border-radius:6px;cursor:pointer;margin-top:8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px}}
figure{{margin:0;background:#1e1b26;border-radius:10px;overflow:hidden;border:1px solid #2e2a3a}}
figure img{{width:100%;max-height:200px;object-fit:contain;background:#0d0b12;display:block}}
figcaption{{font-size:12px;padding:8px;color:#9a93ad}}
.tag{{display:inline-block;background:#2a2436;color:#b9a8d0;border-radius:4px;padding:1px 6px;
margin:0 3px 3px 0;font-size:10px}}
.msg{{color:#7dd87d;font-size:13px;margin-left:10px}}
pre{{background:#1b1823;border:1px solid #332c42;border-radius:8px;padding:12px;overflow:auto;max-height:60vh}}
</style></head><body>
<h1>bot-mindscape 管理台</h1>
<div class=\"tabs\">
<button class=\"on\" onclick=\"show(0,this)\">配置</button>
<button onclick=\"show(1,this)\">图库</button>
<button onclick=\"show(2,this)\">记忆</button>
</div>
<div class=\"panel on\">
<textarea id=\"cfg\">{config}</textarea>
<button class=\"btn\" onclick=\"saveCfg()\">保存配置</button>
<span class=\"msg\" id=\"cmsg\"></span>
</div>
<div class=\"panel\"><div class=\"grid\">{gallery}</div></div>
<div class=\"panel\"><pre>{memory}</pre></div>
<script>
function show(i,btn){{
  document.querySelectorAll('.panel').forEach((p,n)=>p.classList.toggle('on',n===i));
  document.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
}}
async function saveCfg(){{
  const r = await fetch('/api/config',{{method:'POST',headers:{{'X-Mindscape-Token':'{token}'}},body:document.getElementById('cfg').value}});
  const j = await r.json();
  document.getElementById('cmsg').textContent = j.ok ? '已保存 ✓' : ('失败: ' + j.error);
}}
</script></body></html>"""


def render():
    cp = config_path()
    raw = read_text(cp) if os.path.exists(cp) else "# 还没有配置文件\n# 复制 config/config.example.yaml 过来，或直接在这里写\n"

    # 图库
    parts = []
    try:
        with open(index_path(), encoding="utf-8") as f:
            idx = json.load(f)
    except Exception:
        idx = []
    d = stickers_dir()
    for it in (idx if isinstance(idx, list) else []):
        fn = str(it.get("file") or "")
        if not fn:
            continue
        tags = "".join('<span class="tag">%s</span>' % html.escape(str(t)) for t in (it.get("tags") or [])[:5])
        parts.append(
            '<figure><img loading="lazy" src="/img/%s"><figcaption><b>%s</b>'
            '<div style="font-size:10px;color:#5d5768">%s</div>%s<p style="margin:4px 0 0;font-size:11px">%s</p>'
            '</figcaption></figure>' % (
                urllib.parse.quote(fn), html.escape(str(it.get("name") or "?")),
                html.escape(str(it.get("category") or "")), tags,
                html.escape(str(it.get("desc") or ""))))
    gallery = "".join(parts) or '<p style="color:#6f6880">图库还是空的</p>'

    # 记忆
    mem_parts = []
    s = (cfg.section("memory") if cfg else {}) or {}
    for b in (s.get("bots") or []):
        dp = _abs(b.get("diary"))
        mem_parts.append("### %s\n%s" % (b.get("name") or b.get("self_id"),
                                           read_text(dp, 4000) if dp else "(未配置)"))
    memory = "\n\n".join(mem_parts) or "(未配置记忆文件)"

    return PAGE.format(config=html.escape(raw), gallery=gallery, memory=html.escape(memory), token=TOKEN)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/img/"):
            from mindscape_core import safe_name, is_inside, load_index
            fn = safe_name(urllib.parse.unquote(path[5:]))
            if not fn:
                self._send(400, "bad name", "text/plain")
                return
            # 只允许索引里登记过的图片，避免暴露 index.json 等目录内文件
            idx = load_index(index_path()) or []
            known = {str(x.get("file")) for x in idx if isinstance(x, dict)}
            if fn not in known:
                self._send(404, "not found", "text/plain")
                return
            full = os.path.join(stickers_dir(), fn)
            if os.path.exists(full) and is_inside(full, stickers_dir()):
                with open(full, "rb") as f:
                    self._send(200, f.read(), "image/*")
            else:
                self._send(404, "not found", "text/plain")
            return
        self._send(200, render())

    def _same_origin(self):
        """校验 Host 是本机，且 Origin（若有）与 Host 同源。"""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin:
            try:
                o = urllib.parse.urlparse(origin)
                if (o.hostname or "") not in ALLOWED_HOSTS:
                    return False
            except Exception:
                return False
        return True

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/config":
            self._send(404, "{}", "application/json")
            return
        if not self._same_origin():
            self._send(403, json.dumps({"ok": False, "error": "拒绝跨站请求"}), "application/json")
            return
        if self.headers.get("X-Mindscape-Token") != TOKEN:
            self._send(403, json.dumps({"ok": False, "error": "缺少或错误的管理令牌"}), "application/json")
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            self._send(413, json.dumps({"ok": False, "error": "请求体过大"}), "application/json")
            return
        body = self.rfile.read(n).decode("utf-8", "replace")
        if yaml is not None:
            try:
                yaml.safe_load(body)
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": "YAML 语法错误: " + str(e)[:120]}), "application/json")
                return
        cp = config_path()
        try:
            os.makedirs(os.path.dirname(cp) or ".", exist_ok=True)
            if os.path.exists(cp):
                import shutil, datetime
                shutil.copy2(cp, cp + ".bak-" + datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
            with open(cp, "w", encoding="utf-8") as f:
                f.write(body)
            if cfg:
                cfg.load(reload=True)
            self._send(200, json.dumps({"ok": True}), "application/json")
        except Exception as e:
            self._send(200, json.dumps({"ok": False, "error": str(e)[:120]}), "application/json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    print("bot-mindscape 管理台: http://%s:%d" % (a.host, a.port))
    if a.host not in ALLOWED_HOSTS:
        print("⚠️  你正在监听非本机地址 %s —— 该服务没有登录认证，" % a.host)
        print("    仅应在可信内网使用，且务必自行加反向代理认证。")
    else:
        print("（只监听本机，写操作需要页面令牌，Ctrl+C 退出）")
    HTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()