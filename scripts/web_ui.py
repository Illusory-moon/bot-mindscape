# -*- coding: utf-8 -*-
"""mindscape_web —— 可选的本地 Web 管理台

依赖：PyYAML + Python 标准库（http.server）。默认只监听 127.0.0.1。

功能：
  1. 配置编辑（YAML 原文，保存前校验语法）
  2. 图库浏览 / 改名 / 改标签 / 移除
  3. 与远程服务器双向同步（需要配置 ui.sync，见 mindscape_sync.py）

安全说明：写操作要求同源 + 页面令牌；换 --host 对外监听时请自加认证。
"""
import argparse
import html
import json
import os
import secrets
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "plugins"))

try:
    import mindscape_config as cfg
except Exception:
    cfg = None

try:
    import yaml
except ImportError:
    yaml = None

try:
    import mindscape_webedit as edit
except Exception:
    edit = None

TOKEN = secrets.token_urlsafe(16)
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_BODY = 512 * 1024


def _abs(path):
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    base = os.path.dirname(cfg.config_path()) if cfg else HERE
    return os.path.join(base, path)


def config_path():
    return cfg.config_path() if cfg else os.path.join(HERE, "config", "config.yaml")


def stickers_dir():
    s = (cfg.section("stickers") if cfg else {}) or {}
    return _abs(s.get("dir") or "./data/stickers")


def index_path():
    s = (cfg.section("stickers") if cfg else {}) or {}
    return _abs(s.get("index") or os.path.join(stickers_dir(), "index.json"))


def read_text(path, limit=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            t = f.read()
        return t[:limit] if limit else t
    except Exception as e:
        return "(读取失败: %s)" % str(e)[:100]


PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>bot-mindscape 管理台</title><style>
body{{background:#14121a;color:#eee;font-family:system-ui,sans-serif;margin:0;padding:20px}}
h1{{font-size:18px;color:#e85a9b;margin:0 0 12px}}
.tabs button{{background:#241f30;color:#bbb;border:1px solid #332c42;padding:6px 14px;
margin-right:6px;border-radius:6px;cursor:pointer}}
.tabs button.on{{background:#e85a9b;color:#fff;border-color:#e85a9b}}
.bar{{margin:12px 0}}
.bar button{{background:#2c2438;color:#ddd;border:1px solid #3d3450;padding:6px 12px;
border-radius:6px;cursor:pointer;margin-right:6px}}
.bar button:hover{{border-color:#e85a9b}}
.panel{{display:none}}.panel.on{{display:block}}
textarea{{width:100%;height:52vh;background:#1b1823;color:#ddd;border:1px solid #332c42;
border-radius:8px;padding:12px;font-family:monospace;font-size:13px;box-sizing:border-box}}
.btn{{background:#e85a9b;color:#fff;border:0;padding:8px 18px;border-radius:6px;cursor:pointer;margin-top:8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}}
figure{{margin:0;background:#1e1b26;border-radius:10px;overflow:hidden;border:1px solid #2e2a3a}}
figure img{{width:100%;max-height:190px;object-fit:contain;background:#0d0b12;display:block}}
figcaption{{font-size:12px;padding:8px;color:#9a93ad}}
.tag{{display:inline-block;background:#2a2436;color:#b9a8d0;border-radius:4px;padding:1px 6px;
margin:0 3px 3px 0;font-size:10px}}
.row{{margin-top:6px}}
.row button{{font-size:11px;padding:3px 8px;margin-right:4px;background:#2c2438;color:#ccc;
border:1px solid #3d3450;border-radius:4px;cursor:pointer}}
.msg{{color:#7dd87d;font-size:13px;margin-left:10px}}
pre{{background:#1b1823;border:1px solid #332c42;border-radius:8px;padding:12px;overflow:auto;max-height:60vh}}
</style></head><body>
<h1>bot-mindscape 管理台</h1>
<div class="tabs">
<button class="on" onclick="show(0,this)">配置</button>
<button onclick="show(1,this)">图库</button>
<button onclick="show(2,this)">记忆</button>
</div>

<div class="panel on">
<textarea id="cfg">{config}</textarea>
<button class="btn" onclick="saveCfg()">保存配置</button>
<span class="msg" id="cmsg"></span>
</div>

<div class="panel">
<div class="bar">
<button onclick="doSync('status')">查看差异</button>
<button onclick="doSync('pull')">从服务器拉取</button>
<button onclick="doSync('push')">推送到服务器</button>
<span class="msg" id="smsg">{syncmsg}</span>
</div>
<div class="grid">{gallery}</div>
</div>

<div class="panel"><pre>{memory}</pre></div>

<script>
function show(i,btn){{
  document.querySelectorAll('.panel').forEach((p,n)=>p.classList.toggle('on',n===i));
  document.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
}}
const TK = '{token}';
async function post(url, body){{
  const r = await fetch(url, {{method:'POST', headers:{{'X-Mindscape-Token':TK}}, body:body}});
  return await r.json();
}}
async function saveCfg(){{
  const j = await post('/api/config', document.getElementById('cfg').value);
  document.getElementById('cmsg').textContent = j.ok ? '已保存' : ('失败: ' + j.error);
}}
async function doSync(a){{
  document.getElementById('smsg').textContent = '处理中...';
  const j = await post('/api/sync/' + a, '');
  document.getElementById('smsg').textContent = j.ok ? j.message : ('失败: ' + j.error);
  if (a !== 'status') setTimeout(()=>location.reload(), 900);
}}
async function editItem(cat, file){{
  const name = prompt('名称：');
  if (name === null) return;
  const tags = prompt('标签（空格分隔）：');
  if (tags === null) return;
  const desc = prompt('描述：');
  if (desc === null) return;
  const j = await post('/api/stickers/edit', JSON.stringify({{category:cat, file:file, name:name, tags:tags, desc:desc}}));
  alert(j.ok ? j.message : ('失败: ' + j.error));
  if (j.ok) location.reload();
}}
async function removeItem(cat, file){{
  if (!confirm('移除「' + file + '」？（图片文件会保留）')) return;
  const j = await post('/api/stickers/remove', JSON.stringify({{category:cat, file:file}}));
  alert(j.ok ? j.message : ('失败: ' + j.error));
  if (j.ok) location.reload();
}}
</script></body></html>"""


def render():
    cp = config_path()
    if os.path.exists(cp):
        raw = read_text(cp)
    else:
        raw = "# 还没有配置文件\n# 复制 config/config.example.yaml 过来，或直接在这里写\n"

    parts = []
    idx = []
    try:
        with open(index_path(), encoding="utf-8") as f:
            idx = json.load(f)
    except Exception:
        idx = []
    if not isinstance(idx, list):
        idx = []
    for it in idx:
        fn = str(it.get("file") or "")
        if not fn:
            continue
        cat = str(it.get("category") or "")
        tags = "".join('<span class="tag">%s</span>' % html.escape(str(t)) for t in (it.get("tags") or [])[:5])
        esc_cat = html.escape(cat, quote=True)
        esc_fn = html.escape(fn, quote=True)
        parts.append(
            '<figure><img loading="lazy" src="/img/%s"><figcaption><b>%s</b>'
            '<div style="font-size:10px;color:#5d5768">%s</div>%s'
            '<p style="margin:4px 0 0;font-size:11px">%s</p>'
            '<div class="row"><button onclick="editItem(\'%s\',\'%s\')">编辑</button>'
            '<button onclick="removeItem(\'%s\',\'%s\')">移除</button></div>'
            '</figcaption></figure>' % (
                urllib.parse.quote(fn), html.escape(str(it.get("name") or "?")),
                html.escape(cat), tags, html.escape(str(it.get("desc") or "")),
                esc_cat, esc_fn, esc_cat, esc_fn))
    gallery = "".join(parts) or '<p style="color:#6f6880">图库还是空的</p>'

    mem_parts = []
    s = (cfg.section("memory") if cfg else {}) or {}
    for b in (s.get("bots") or []):
        dp = _abs(b.get("diary"))
        mem_parts.append("### %s\n%s" % (b.get("name") or b.get("self_id"),
                                         read_text(dp, 4000) if dp else "(未配置)"))
    memory = "\n\n".join(mem_parts) or "(未配置记忆文件)"

    sync_ok = bool(edit) and edit.sync_available()
    syncmsg = "" if sync_ok else "（未配置 ui.sync，同步功能不可用）"
    return PAGE.format(config=html.escape(raw), gallery=gallery, memory=html.escape(memory),
                       token=TOKEN, syncmsg=html.escape(syncmsg))


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

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json")

    def _same_origin(self):
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

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return None
        return self.rfile.read(n).decode("utf-8", "replace")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/img/"):
            from mindscape_core import is_inside, load_index, safe_name
            fn = safe_name(urllib.parse.unquote(path[5:]))
            if not fn:
                self._send(400, "bad name", "text/plain")
                return
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

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._same_origin():
            self._json({"ok": False, "error": "拒绝跨站请求"}, 403)
            return
        if self.headers.get("X-Mindscape-Token") != TOKEN:
            self._json({"ok": False, "error": "缺少或错误的管理令牌"}, 403)
            return
        body = self._body()
        if body is None:
            self._json({"ok": False, "error": "请求体过大"}, 413)
            return

        if path == "/api/config":
            if yaml is not None:
                try:
                    yaml.safe_load(body)
                except Exception as e:
                    self._json({"ok": False, "error": "YAML 语法错误: " + str(e)[:120]})
                    return
            cp = config_path()
            try:
                os.makedirs(os.path.dirname(cp) or ".", exist_ok=True)
                if os.path.exists(cp):
                    import datetime
                    import shutil
                    shutil.copy2(cp, cp + ".bak-" + datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
                with open(cp, "w", encoding="utf-8") as f:
                    f.write(body)
                if cfg:
                    cfg.load(reload=True)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)[:120]})
            return

        if path == "/api/stickers/edit":
            if not edit:
                self._json({"ok": False, "error": "编辑模块不可用"})
                return
            try:
                d = json.loads(body or "{}")
            except Exception:
                self._json({"ok": False, "error": "参数不是合法 JSON"})
                return
            ok, msg = edit.edit_item(d.get("category"), d.get("file"),
                                     d.get("name"), d.get("tags"), d.get("desc"))
            self._json({"ok": ok, "message" if ok else "error": msg})
            return

        if path == "/api/stickers/remove":
            if not edit:
                self._json({"ok": False, "error": "编辑模块不可用"})
                return
            try:
                d = json.loads(body or "{}")
            except Exception:
                self._json({"ok": False, "error": "参数不是合法 JSON"})
                return
            ok, msg = edit.remove_item(d.get("category"), d.get("file"),
                                       bool(d.get("delete_file")))
            self._json({"ok": ok, "message" if ok else "error": msg})
            return

        if path.startswith("/api/sync/"):
            if not edit:
                self._json({"ok": False, "error": "同步模块不可用"})
                return
            action = path.rsplit("/", 1)[-1]
            ok, msg = edit.do_sync(action)
            self._json({"ok": ok, "message" if ok else "error": msg})
            return

        self._json({"ok": False, "error": "未知接口"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    print("bot-mindscape 管理台: http://%s:%d" % (a.host, a.port))
    if a.host not in ALLOWED_HOSTS:
        print("警告：正在监听非本机地址 %s —— 该服务只有页面令牌，请自加认证。" % a.host)
    else:
        print("（只监听本机，写操作需要页面令牌，Ctrl+C 退出）")
    HTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
