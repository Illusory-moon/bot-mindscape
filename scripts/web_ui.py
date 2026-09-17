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


JS = r"""const TK = '@@TOKEN@@';
const ITEMS = @@ITEMS@@;
const K_TAB = 'mindscape.tab';
const K_CFG = 'mindscape.draft.config';
const K_ITEM = 'mindscape.draft.item.';
let CUR = null;

// 草稿存取：写不进去也只是丢草稿，绝不能让功能挂掉
function ls(k, v){
  try {
    if (v === undefined) return localStorage.getItem(k);
    if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v);
  } catch (e) {}
  return null;
}

function show(i, btn){
  document.querySelectorAll('.panel').forEach((p,n)=>p.classList.toggle('on',n===i));
  document.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('on'));
  const b = btn || document.querySelectorAll('.tabs button')[i];
  if (b) b.classList.add('on');
  ls(K_TAB, String(i));
}

async function post(url, body){
  const r = await fetch(url, {method:'POST', headers:{'X-Mindscape-Token':TK}, body:body});
  return await r.json();
}

// ── 配置页：边打边存草稿，浏览器丢标签页 / 误刷新都不会白改 ──
function cfgDirty(){
  const el = document.getElementById('cfg');
  return !!(el && el.value !== el.defaultValue);
}
function cfgRestore(){
  const el = document.getElementById('cfg');
  const d = ls(K_CFG);
  if (d !== null && d !== el.value){
    el.value = d;
    document.getElementById('cmsg').textContent = '（已恢复上次没保存的草稿）';
  }
}
async function saveCfg(){
  const el = document.getElementById('cfg');
  const j = await post('/api/config', el.value);
  document.getElementById('cmsg').textContent = j.ok ? '已保存' : ('失败: ' + j.error);
  if (j.ok){ el.defaultValue = el.value; ls(K_CFG, null); }
}

// ── 图库：弹窗编辑，保存后【就地更新卡片】，不刷新页面 ──
function updateCard(it){
  const fig = document.querySelector('figure[data-i="' + it.i + '"]');
  if (!fig) return;
  const n = fig.querySelector('.cname'); if (n) n.textContent = it.name || '?';
  const d = fig.querySelector('.cdesc'); if (d) d.textContent = it.desc || '';
  const t = fig.querySelector('.ctags');
  if (t){
    t.innerHTML = (it.tags || '').split(/\s+/).filter(x=>x).slice(0,5)
      .map(x=>'<span class="tag">' + x.replace(/[<>&"]/g,'') + '</span>').join('');
  }
}
function editItem(cat, file){
  const it = ITEMS.find(x => x.cat === cat && x.file === file);
  if (!it) return;
  CUR = it;
  let d = null;
  const draft = ls(K_ITEM + cat + '|' + file);
  if (draft){ try { d = JSON.parse(draft); } catch(e){} }
  document.getElementById('mimg').src = '/img/' + encodeURIComponent(file);
  document.getElementById('mname').value = (d && d.name) || it.name;
  document.getElementById('mtags').value = (d && d.tags) || it.tags;
  document.getElementById('mdesc').value = (d && d.desc) || it.desc;
  document.getElementById('mmsg').textContent = d ? '（已恢复未保存的改动）' : '';
  document.getElementById('modal').classList.add('on');
  document.getElementById('mname').focus();
}
function closeModal(){
  document.getElementById('modal').classList.remove('on');
  CUR = null;
}
function mDraft(){
  if (!CUR) return;
  ls(K_ITEM + CUR.cat + '|' + CUR.file, JSON.stringify({
    name: document.getElementById('mname').value,
    tags: document.getElementById('mtags').value,
    desc: document.getElementById('mdesc').value
  }));
}
async function saveItem(){
  if (!CUR) return;
  const it = CUR;
  const vals = {
    category: it.cat, file: it.file,
    name: document.getElementById('mname').value,
    tags: document.getElementById('mtags').value,
    desc: document.getElementById('mdesc').value
  };
  const j = await post('/api/stickers/edit', JSON.stringify(vals));
  document.getElementById('mmsg').textContent = j.ok ? '已保存' : ('失败: ' + j.error);
  if (!j.ok) return;
  ls(K_ITEM + it.cat + '|' + it.file, null);
  it.name = vals.name; it.tags = vals.tags; it.desc = vals.desc;
  updateCard(it);
  setTimeout(closeModal, 450);
}
async function removeItem(cat, file){
  if (!confirm('移除「' + file + '」？（图片文件会保留）')) return;
  const j = await post('/api/stickers/remove', JSON.stringify({category:cat, file:file}));
  if (!j.ok){ alert('失败: ' + j.error); return; }
  const it = ITEMS.find(x => x.cat === cat && x.file === file);
  const fig = it ? document.querySelector('figure[data-i="' + it.i + '"]') : null;
  if (fig) fig.remove();
}
async function doSync(a){
  document.getElementById('smsg').textContent = '处理中...';
  const j = await post('/api/sync/' + a, '');
  document.getElementById('smsg').textContent = j.ok ? j.message : ('失败: ' + j.error);
  if (a !== 'status' && j.ok) setTimeout(()=>location.reload(), 900);
}

// ── 启动 ──
['mname','mtags','mdesc'].forEach(id => document.getElementById(id).addEventListener('input', mDraft));
document.getElementById('cfg').addEventListener('input', function(){
  ls(K_CFG, document.getElementById('cfg').value);
});
document.getElementById('modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
// 真有没保存的东西时，拦一下刷新/关闭
window.addEventListener('beforeunload', e => {
  if (cfgDirty() || CUR){ e.preventDefault(); e.returnValue = ''; }
});
cfgRestore();
show(parseInt(ls(K_TAB) || '0', 10) || 0, null);
"""


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
h2.cat{{font-size:14px;color:#c9bfe0;margin:20px 0 10px;padding-bottom:6px;border-bottom:1px solid #2e2a3a}}
h2.cat:first-child{{margin-top:4px}}
h2.cat span{{color:#6f6880;font-weight:400;font-size:12px;margin-left:8px}}
.hint{{color:#8b83a0;font-size:12px;line-height:1.8;background:#1b1823;border:1px solid #332c42;
border-radius:8px;padding:10px 12px;margin:10px 0}}
code{{background:#2a2436;padding:1px 6px;border-radius:4px;color:#e9b7d4;font-size:12px}}
.modal{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:50;
align-items:center;justify-content:center}}
.modal.on{{display:flex}}
.mbox{{background:#1e1b26;border:1px solid #3d3450;border-radius:12px;padding:18px;
width:470px;max-width:92vw;max-height:90vh;overflow:auto}}
.mbox h3{{margin:0 0 12px;color:#e85a9b;font-size:15px}}
.mbox img{{width:100%;max-height:180px;object-fit:contain;background:#0d0b12;border-radius:8px}}
.mbox label{{display:block;font-size:12px;color:#9a93ad;margin:12px 0 4px}}
.mbox input,.mbox textarea{{width:100%;box-sizing:border-box;background:#14121a;color:#eee;
border:1px solid #3d3450;border-radius:6px;padding:7px 9px;font-size:13px;font-family:inherit}}
.mbox textarea{{height:70px;resize:vertical}}
.mrow{{margin-top:14px;display:flex;align-items:center}}
.btn2{{background:#2c2438;color:#ddd;border:1px solid #3d3450;padding:8px 18px;
border-radius:6px;cursor:pointer;margin-top:8px;margin-left:8px}}
.btn2:hover{{border-color:#e85a9b}}
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
<p class="hint">图库目录：<code>{gdir}</code>　共 <b>{gcount}</b> 张，按分类分组</p>
{gallery}
</div>

<div class="panel"><pre>{memory}</pre></div>

<div class="modal" id="modal">
  <div class="mbox">
    <h3>编辑表情包</h3>
    <img id="mimg" alt="">
    <label>名称</label>
    <input id="mname" placeholder="给它起个名">
    <label>标签（空格分隔）</label>
    <input id="mtags" placeholder="无语 嫌弃 敷衍">
    <label>描述</label>
    <textarea id="mdesc" placeholder="什么场合适合用这张"></textarea>
    <div class="mrow">
      <button class="btn" onclick="saveItem()">保存</button>
      <button class="btn2" onclick="closeModal()">关闭</button>
      <span class="msg" id="mmsg"></span>
    </div>
  </div>
</div>

<script>__JS__</script></body></html>"""


def render():
    cp = config_path()
    if os.path.exists(cp):
        raw = read_text(cp)
    else:
        raw = "# 还没有配置文件\n# 复制 config/config.example.yaml 过来，或直接在这里写\n"

    idx = []
    try:
        with open(index_path(), encoding="utf-8") as f:
            idx = json.load(f)
    except Exception:
        idx = []
    if not isinstance(idx, list):
        idx = []
    by_cat = {}
    for it in idx:
        if str(it.get("file") or ""):
            by_cat.setdefault(str(it.get("category") or ""), []).append(it)

    # 编辑弹窗要拿「当前值」；顺便给每张卡片编号，保存后好【就地更新】
    # （以前 saveItem 里是 location.reload()，刷新会跳回第一个标签页、还会丢草稿）
    items = []
    item_index = {}
    for it in idx:
        fn = str(it.get("file") or "")
        if not fn:
            continue
        cat = str(it.get("category") or "")
        item_index[(cat, fn)] = len(items)
        items.append({
            "i": len(items), "cat": cat, "file": fn,
            "name": str(it.get("name") or ""),
            "tags": " ".join(str(x) for x in (it.get("tags") or [])),
            "desc": str(it.get("desc") or ""),
        })
    items_js = json.dumps(items, ensure_ascii=False)

    def figure(it):
        fn = str(it.get("file") or "")
        cat = str(it.get("category") or "")
        tags = "".join('<span class="tag">%s</span>'
                       % html.escape(str(t)) for t in (it.get("tags") or [])[:5])
        esc_cat = html.escape(cat, quote=True)
        esc_fn = html.escape(fn, quote=True)
        n = item_index.get((cat, fn), -1)
        return (
            '<figure data-i="%d"><img loading="lazy" src="/img/%s">'
            '<figcaption><b class="cname">%s</b>'
            '<div style="font-size:10px;color:#5d5768">%s</div>'
            '<span class="ctags">%s</span>'
            '<p class="cdesc" style="margin:4px 0 0;font-size:11px">%s</p>'
            '<div class="row"><button onclick="editItem(\'%s\',\'%s\')">编辑</button>'
            '<button onclick="removeItem(\'%s\',\'%s\')">移除</button></div>'
            '</figcaption></figure>' % (
                n, urllib.parse.quote(fn), html.escape(str(it.get("name") or "?")),
                html.escape(cat), tags, html.escape(str(it.get("desc") or "")),
                esc_cat, esc_fn, esc_cat, esc_fn))

    # 每个分类一个标题 + 一个网格：不同 bot 的图库一眼分得开
    blocks = []
    for cat in sorted(by_cat):
        blocks.append(
            '<h2 class="cat">%s<span>%d 张</span></h2><div class="grid">%s</div>'
            % (html.escape(cat or "(未分类)"), len(by_cat[cat]),
               "".join(figure(it) for it in by_cat[cat])))
    gallery = "".join(blocks) or '<p style="color:#6f6880">图库还是空的</p>'
    gcount = sum(len(v) for v in by_cat.values())


    # 记忆页：本地配置通常没有 memory.bots（长期记忆在 bot 所在的那台机器上），
    # 空着是正常的 —— 把「为什么空」和「正在读哪个文件」直接写出来。
    mem_parts = []
    s = (cfg.section("memory") if cfg else {}) or {}
    for b in (s.get("bots") or []):
        dp = _abs(b.get("diary"))
        mem_parts.append("### %s\n%s" % (b.get("name") or b.get("self_id"),
                                         read_text(dp, 4000) if dp else "(未配置 diary)"))
    if mem_parts:
        memory = "\n\n".join(mem_parts)
    else:
        memory = (
            "这个标签页会列出 memory.bots 里每个 bot 的长期记忆文件，方便直接翻看。\n\n"
            "当前「memory.bots」是空的 —— 本地这边只管图库，长期记忆在 bot 所在的那台\n"
            "机器上，所以这里空着是正常的。想在本地看记忆，就在「配置」页加一条：\n\n"
            "memory:\n"
            "  bots:\n"
            "    - self_id: \"20000000\"\n"
            "      name: \"bot-name\"\n"
            "      diary: \"/path/to/bot-name.md\"\n\n"
            "当前读取的配置文件：" + config_path())

    sync_ok = bool(edit) and edit.sync_available()
    syncmsg = "" if sync_ok else "（未配置 ui.sync，同步功能不可用）"
    page = PAGE.format(config=html.escape(raw), gallery=gallery, memory=html.escape(memory),
                       token=TOKEN, syncmsg=html.escape(syncmsg),
                       gdir=html.escape(stickers_dir()), gcount=gcount)
    # JS 走占位符注入，不进 str.format —— 否则 JS 里每个花括号都要手写双份
    return page.replace("__JS__", JS.replace("@@TOKEN@@", TOKEN)
                                 .replace("@@ITEMS@@", items_js))


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
