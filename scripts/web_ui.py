# -*- coding: utf-8 -*-
"""mindscape_web —— 可选的本地 Web 管理台

依赖：PyYAML + Python 标准库（http.server）。默认只监听 127.0.0.1。

功能：
  1. 分区配置表单（本地自动保存，检查远端版本后同步）
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

try:
    import mindscape_webconfig as managed
except Exception:
    managed = None

try:
    import mindscape_observe as observe
except Exception:
    observe = None

try:
    import check_live as live
except Exception:
    live = None

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


def seen_path():
    s = (cfg.section("stickers") if cfg else {}) or {}
    return _abs(s.get("seen") or os.path.join(stickers_dir(), "seen.json"))


def load_seen():
    try:
        with open(seen_path(), encoding="utf-8") as f:
            d = json.load(f)
        return [str(k) for k in d] if isinstance(d, list) else []
    except Exception:
        return []


def save_seen(keys):
    import datetime
    import shutil
    p = seen_path()
    if os.path.exists(p):
        shutil.copy2(p, p + ".bak-" + datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(set(keys)), f)
    os.replace(tmp, p)


def seen_rows():
    """把去重表摊开给主人看。

    bot 用它判断「这张图处理过了，别再折腾」。**删图库条目时它不会跟着删**，
    于是留下一条墓碑：那张图再发一次也收不进来。这里把墓碑标出来，
    既能单独解除（我想让它重新判一次），也能一键清理（删错了想收回来）。
    """
    keys = load_seen()
    try:
        with open(index_path(), encoding="utf-8") as f:
            idx = json.load(f)
    except Exception:
        idx = []
    name_of = {}
    for it in (idx if isinstance(idx, list) else []):
        fn = str(it.get("file") or "")
        if fn:
            name_of[str(it.get("category") or "") + ":" + fn[:10]] = \
                str(it.get("name") or fn)
    rows = []
    for k in keys:
        cat, _, h = k.partition(":")
        hit = name_of.get(cat + ":" + h[:10])
        rows.append({"key": k, "cat": cat, "live": bool(hit),
                     "label": hit or "已不在图库（墓碑）"})
    rows.sort(key=lambda r: (r["live"], r["cat"], r["key"]))
    return rows


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
let CONFIG = null;
let BOT = null;
let SAVE_QUEUE = Promise.resolve();
const PENDING_SAVES = new Map();
const OBSERVED = {memory:[], logs:[]};

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

function configStatus(message, error=false){
  const el = document.getElementById('managed-msg');
  el.textContent = message;
  el.classList.toggle('error', error);
}
async function loadManaged(){
  configStatus('正在读取配置...');
  try {
    const j = await post('/api/managed/view', '');
    if (!j.ok) throw Error(j.error);
    CONFIG = j.data;
    const select = document.getElementById('bot-select');
    select.replaceChildren();
    CONFIG.bots.forEach(b => {
      const option = document.createElement('option');
      option.value = b.id; option.textContent = b.name + ' · ' + b.id;
      select.append(option);
    });
    BOT = CONFIG.bots.some(b => b.id === BOT) ? BOT : CONFIG.bots[0]?.id;
    select.value = BOT || '';
    renderFields();
    configStatus(CONFIG.dirty ? '已保存到本机，等待同步服务器' : '本机与服务器一致');
  } catch(e) { configStatus('读取失败：' + e.message, true); }
}
function renderFields(){
  document.querySelectorAll('.config-section').forEach(section => {
    section.replaceChildren();
    const tab = section.dataset.tab;
    const fields = (CONFIG?.fields || []).filter(f => f.tab === tab);
    fields.forEach(f => {
      const row = document.createElement('div');
      row.className = f.type === 'lines' ? 'field field-lines' : 'field';
      const text = document.createElement('div');
      const title = document.createElement('label'); title.textContent = f.label;
      const hint = document.createElement('small'); hint.textContent = f.hint || '';
      text.append(title, hint);
      const input = document.createElement(f.type === 'lines' ? 'textarea' : 'input');
      const key = f.source + ':' + f.path;
      input.id = 'setting-' + f.source + '-' + f.path.replaceAll('.', '-');
      title.htmlFor = input.id;
      const owner = f.scope === 'bot' ? BOT : 'global';
      let value = CONFIG.values[owner]?.[key];
      if (f.type === 'bool' || f.type === 'target') {
        input.type = 'checkbox'; input.checked = !!value;
      } else if (f.type === 'int' || f.type === 'prob') {
        input.type = 'number'; input.min = '0';
        input.max = f.type === 'prob' ? '100' : '1000000';
        input.step = f.type === 'prob' ? 'any' : '1';
        input.required = true;
        input.value = value ?? '';
      } else { input.value = value ?? ''; input.rows = 4; }
      if (f.scope === 'bot' && !BOT) input.disabled = true;
      let timer;
      const save = () => {
        const v = (f.type === 'bool' || f.type === 'target') ? input.checked
          : f.type === 'int' ? Number(input.value)
          : f.type === 'prob' ? Number(input.value) : input.value;
        if (!input.checkValidity()) { configStatus('请检查「' + f.label + '」的数值', true); return; }
        configStatus('正在保存到本机...');
        SAVE_QUEUE = SAVE_QUEUE.then(async () => {
          const j = await post('/api/managed/change', JSON.stringify({source:f.source,path:f.path,bot:owner,value:v}));
          if (!j.ok) throw Error(j.error);
          CONFIG.dirty = j.dirty;
          CONFIG.values[owner][key] = v;
          configStatus('已保存到本机，等待同步服务器');
        }).catch(e => configStatus('保存失败：' + e.message, true));
      };
      input.addEventListener(f.type === 'bool' || f.type === 'target' ? 'change' : 'input', () => {
        clearTimeout(timer);
        PENDING_SAVES.delete(input);
        timer = setTimeout(() => { PENDING_SAVES.delete(input); save(); },
                           f.type === 'bool' || f.type === 'target' ? 0 : 500);
        PENDING_SAVES.set(input, () => { clearTimeout(timer); save(); });
      });
      row.append(text, input); section.append(row);
    });
  });
}
function configTab(n, button){
  document.querySelectorAll('.config-section').forEach((el,i) => el.classList.toggle('on',i===n));
  document.querySelectorAll('.config-tabs button').forEach(el => el.classList.remove('on'));
  button.classList.add('on');
}
async function configSync(action){
  for (const flush of PENDING_SAVES.values()) flush();
  PENDING_SAVES.clear();
  await SAVE_QUEUE;
  if (action === 'pull' && CONFIG?.dirty && !confirm('本机有未同步的修改，重新读取会覆盖它们。继续吗？')) return;
  if (action === 'push' && !CONFIG?.dirty) { configStatus('本机与服务器一致'); return; }
  configStatus(action === 'push' ? '正在同步服务器...' : '正在从服务器读取...');
  try {
    const j = await post('/api/managed/' + action, '');
    if (!j.ok) throw Error(j.error);
    await loadManaged();
    configStatus(j.message);
  } catch(e) { configStatus('同步失败：' + e.message, true); }
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

async function observeList(kind){
  const select = document.getElementById(kind + '-files');
  const selected = select.value;
  const status = document.getElementById(kind + '-msg');
  try {
    const j = await post('/api/observe/' + kind + '/list', '');
    if (!j.ok) throw Error(j.error);
    OBSERVED[kind] = j.files;
    select.replaceChildren();
    j.files.forEach((item, i) => {
      const option = document.createElement('option');
      option.value = String(i);
      option.textContent = item.label + ' · ' + item.path + (item.error ? ' · ' + item.error : '');
      select.append(option);
    });
    if (j.files.length) { select.value = selected && Number(selected) < j.files.length ? selected : '0'; await observeRead(kind); }
    else { document.getElementById(kind + '-text').textContent = ''; document.getElementById(kind + '-meta').textContent = ''; }
    if (!j.files.length) status.textContent = '暂无本机快照';
  } catch(e) { status.textContent = '读取失败：' + e.message; }
}
async function observeRead(kind){
  const select = document.getElementById(kind + '-files');
  const item = OBSERVED[kind][Number(select.value)];
  if (!item) return;
  const meta = document.getElementById(kind + '-meta');
  const age = item.mtime ? (Date.now() / 1000 - item.mtime) : 0;
  meta.textContent = item.missing ? '尚未生成' : item.error ? '读取失败：' + item.error
    : item.mtime ? '服务器更新：' + new Date(item.mtime * 1000).toLocaleString() + ' · ' + item.size.toLocaleString() + ' 字节'
      + (age > 48 * 3600 ? ' · 超过 48 小时未更新（空闲通道可能正常）' : '')
    : item.size.toLocaleString() + ' 字节 · 服务器时间未记录';
  const pre = document.getElementById(kind + '-text');
  if (!item.id) { pre.textContent = ''; return; }
  const j = await post('/api/observe/' + kind + '/read', JSON.stringify({id:item.id}));
  pre.textContent = j.ok ? j.text : ('读取失败：' + j.error);
  document.getElementById(kind + '-msg').textContent = j.ok && j.truncated ? '显示末尾 200 KB；完整文件已保存到本机' : '';
}

async function loadChannels(){
  const status = document.getElementById('channels-msg');
  const select = document.getElementById('channel-select');
  status.textContent = '正在核对服务器...';
  try {
    const j = await post('/api/channels/check', JSON.stringify({text_channel:select.value}));
    if (!j.ok) throw Error(j.error);
    const chosen = select.value || j.text_channel;
    select.replaceChildren();
    const none = document.createElement('option'); none.value = ''; none.textContent = '选择文字通道'; select.append(none);
    j.channels.forEach(c => { const o = document.createElement('option'); o.value = c.id; o.textContent = c.name + ' · ' + c.id; select.append(o); });
    select.value = chosen;
    const body = document.getElementById('channel-rows'); body.replaceChildren();
    const fields = ['format','no_period','diary','digest','archive','stickers','vision','groupctx'];
    j.channels.forEach(c => {
      const tr = document.createElement('tr');
      [c.name + ' · ' + c.id, ...fields.map(f => c.targets[f] ? '已列入' : '未列入'),
       c.persona + (c.bindings === null ? '' : ' (' + c.bindings + ' 会话)') + (c.expected_persona ? ' · 预期 ' + c.expected_persona : '')].forEach(v => {
        const td = document.createElement('td'); td.textContent = v; tr.append(td);
      });
      if (c.id === chosen) tr.className = 'selected-channel';
      body.append(tr);
    });
    status.textContent = j.issues.length ? j.issues.join('；') : '已检查通道配置与会话人格绑定';
    status.classList.toggle('error', !!j.issues.length);
  } catch(e) { status.textContent = '核对失败：' + e.message; status.classList.add('error'); }
}
async function observePull(kind){
  const status = document.getElementById(kind + '-msg');
  status.textContent = '正在拉取...';
  try {
    const j = await post('/api/observe/' + kind + '/pull', '');
    if (!j.ok) throw Error(j.error);
    await observeList(kind);
    status.textContent = '已拉取 ' + j.count + ' 份' + (j.missing ? '，' + j.missing + ' 份尚未生成' : '')
      + (j.errors ? '，' + j.errors + ' 份失败' : '');
  } catch(e) { status.textContent = '拉取失败：' + e.message; }
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
  if (cfgDirty() || CUR || document.getElementById('managed-msg').textContent === '正在保存到本机...'){
    e.preventDefault(); e.returnValue = '';
  }
});
cfgRestore();
loadManaged();
observeList('memory');
observeList('logs');
loadChannels();
async function rmSeen(btn){
  const tr = btn.closest('tr');
  const j = await post('/api/seen/remove', JSON.stringify({key: tr.dataset.k}));
  const m = document.getElementById('kmsg');
  if (!j.ok) { m.textContent = j.error || '失败'; return; }
  tr.remove();
  m.textContent = '已解除：' + tr.dataset.k;
}

async function pruneSeen(){
  const m = document.getElementById('kmsg');
  const j = await post('/api/seen/prune', '');
  if (!j.ok) { m.textContent = j.error || '失败'; return; }
  document.querySelectorAll('tr.tomb').forEach(r => r.remove());
  m.textContent = j.message || '已清理';
}

show(parseInt(ls(K_TAB) || '0', 10) || 0, null);
"""


PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>bot-mindscape 管理台</title><style>
html{{background:#14181b}}
body{{box-sizing:border-box;max-width:1240px;background:#14181b;color:#edf0ef;font-family:system-ui,sans-serif;margin:0 auto;padding:24px;line-height:1.45}}
body::before{{content:'';display:block;height:4px;background:linear-gradient(90deg,#e76783,#f5b451,#65c4a9);position:fixed;top:0;left:0;right:0}}
h1{{font-size:20px;color:#f3f5f3;margin:0 0 18px}}
button,input,textarea,select{{font:inherit}}
button:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible{{outline:2px solid #f5b451;outline-offset:2px}}
.tabs{{display:flex;flex-wrap:wrap;gap:6px}}
.tabs button{{background:#242b2e;color:#b8c5c3;border:1px solid #3d4a4d;padding:6px 14px;
border-radius:6px;cursor:pointer}}
.tabs button.on{{background:#cb536e;color:#fff;border-color:#cb536e}}
.bar{{margin:12px 0}}
.bar button{{background:#293337;color:#ddd;border:1px solid #48585b;padding:6px 12px;
border-radius:6px;cursor:pointer;margin-right:6px}}
.bar button:hover{{border-color:#e76783}}
.panel{{display:none}}.panel.on{{display:block}}
.config-head{{display:flex;align-items:end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:18px 0}}
.config-head h2{{font-size:19px;margin:0 0 3px}}
.config-head p{{font-size:13px;color:#aab5b3;margin:0}}
.config-head select{{background:#242b2e;color:#fff;border:1px solid #4e5e60;border-radius:5px;padding:8px;min-width:180px}}
.config-tabs{{display:flex;gap:4px;overflow-x:auto;border-bottom:1px solid #394246;margin:0 0 14px}}
.config-tabs button{{white-space:nowrap;background:transparent;color:#aebbb9;border:0;border-bottom:2px solid transparent;padding:10px 18px;cursor:pointer}}
.config-tabs button.on{{color:#fff;border-bottom-color:#e76783}}
.config-section{{display:none}}
.config-section.on{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:28px}}
.field{{display:grid;grid-template-columns:minmax(0,1fr) minmax(150px,.85fr);gap:14px;align-items:center;padding:15px 2px;border-bottom:1px solid #303b3f;min-width:0}}
.field-lines{{grid-column:1/-1;grid-template-columns:minmax(210px,.4fr) minmax(0,1fr)}}
.field label{{display:block;font-size:14px;font-weight:600}}
.field small{{display:block;color:#9baaa8;font-size:12px;margin-top:4px}}
.field input:not([type=checkbox]),.field textarea{{width:100%;box-sizing:border-box;background:#20282b;color:#fff;border:1px solid #48585b;border-radius:5px;padding:8px 10px}}
.field textarea{{height:90px;resize:vertical;font-family:inherit}}
.field input[type=checkbox]{{appearance:none;width:42px;height:24px;background:#536163;border-radius:14px;cursor:pointer;position:relative}}
.field input[type=checkbox]::after{{content:'';position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:white;transition:left .15s}}
.field input[type=checkbox]:checked{{background:#51a98c}}
.field input[type=checkbox]:checked::after{{left:21px}}
.config-actions{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:16px 0}}
.config-actions button{{background:#334044;color:#fff;border:1px solid #55676a;padding:9px 14px;border-radius:5px;cursor:pointer}}
.config-actions button.primary{{background:#cb536e;border-color:#cb536e}}
.config-actions button:hover{{filter:brightness(1.14)}}
.config-actions .msg{{margin:0}}
.msg.error{{color:#ff9c9c}}
.advanced{{margin:24px 0;border-top:1px solid #394246;padding-top:12px;color:#abb7b4}}
.advanced summary{{cursor:pointer}}
.advanced textarea{{margin-top:12px}}
@media(max-width:1000px){{.config-section.on{{grid-template-columns:1fr}}.field-lines{{grid-template-columns:minmax(0,1fr) minmax(190px,1.1fr)}}}}
@media(max-width:600px){{body{{padding:16px}}.field,.field-lines{{grid-template-columns:1fr;gap:8px}}.config-tabs{{flex-wrap:wrap;overflow:visible}}.config-tabs button{{padding:10px 12px}}.config-head label{{width:100%}}.config-head select{{max-width:100%;min-width:0}}}}
textarea{{width:100%;height:52vh;background:#20282b;color:#ddd;border:1px solid #48585b;
border-radius:8px;padding:12px;font-family:monospace;font-size:13px;box-sizing:border-box}}
.btn{{background:#cb536e;color:#fff;border:0;padding:8px 18px;border-radius:6px;cursor:pointer;margin-top:8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}}
figure{{margin:0;background:#20282b;border-radius:7px;overflow:hidden;border:1px solid #39474a}}
figure img{{width:100%;max-height:190px;object-fit:contain;background:#101718;display:block}}
figcaption{{font-size:12px;padding:8px;color:#aebbb9}}
.tag{{display:inline-block;background:#334044;color:#c8d6d3;border-radius:4px;padding:1px 6px;
margin:0 3px 3px 0;font-size:10px}}
.row{{margin-top:6px}}
.row button{{font-size:11px;padding:3px 8px;margin-right:4px;background:#293337;color:#ccc;
border:1px solid #48585b;border-radius:4px;cursor:pointer}}
.msg{{color:#7dd87d;font-size:13px;margin-left:10px}}
pre{{background:#20282b;border:1px solid #48585b;border-radius:7px;padding:12px;overflow:auto;max-height:60vh}}
.observe-select{{display:block;width:100%;max-width:100%;background:#20282b;color:#edf0ef;border:1px solid #48585b;border-radius:5px;padding:8px;margin:10px 0}}
.observe-text{{white-space:pre-wrap;overflow-wrap:anywhere;min-height:180px}}
.observe-meta{{color:#b8c5c3;font-size:13px;min-height:20px}}
.channels{{display:block;width:100%;border-collapse:collapse;font-size:12px;overflow-x:auto;margin-top:12px}}
.channels th,.channels td{{padding:8px 10px;text-align:left;white-space:nowrap;border-bottom:1px solid #394246}}
.channels th{{color:#b8c5c3}}
.channels .selected-channel{{background:#293337}}
h2.cat{{font-size:14px;color:#c9bfe0;margin:20px 0 10px;padding-bottom:6px;border-bottom:1px solid #2e2a3a}}
h2.cat:first-child{{margin-top:4px}}
h2.cat span{{color:#6f6880;font-weight:400;font-size:12px;margin-left:8px}}
.hint{{color:#adbbb8;font-size:12px;line-height:1.8;background:#20282b;border:1px solid #39474a;
border-radius:7px;padding:10px 12px;margin:10px 0}}
code{{background:#2a2436;padding:1px 6px;border-radius:4px;color:#e9b7d4;font-size:12px}}
.modal{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:50;
align-items:center;justify-content:center}}
.modal.on{{display:flex}}
.mbox{{background:#20282b;border:1px solid #48585b;border-radius:7px;padding:18px;
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
table.seen{{display:block;width:100%;border-collapse:collapse;font-size:12px;margin-top:8px;overflow-x:auto}}
table.seen td{{padding:5px 8px;border-bottom:1px solid #2a2436}}
table.seen tr.tomb td{{background:#1d1826;color:#8a7f9c}}
table.seen code{{color:#e85a9b;font-size:11px}}
table.seen button{{background:#2c2438;color:#ddd;border:1px solid #3d3450;
padding:2px 10px;border-radius:5px;cursor:pointer}}
table.seen button:hover{{border-color:#e85a9b}}
</style></head><body>
<h1>bot-mindscape 管理台</h1>
<div class="tabs">
<button class="on" onclick="show(0,this)">配置</button>
<button onclick="show(1,this)">图库</button>
<button onclick="show(2,this)">记忆</button>
<button onclick="show(3,this)">日志</button>
<button onclick="show(4,this)">去重表</button>
<button onclick="show(5,this)">通道</button>
</div>

<div class="panel on">
<div class="config-head"><div><h2>配置</h2><p>调整后自动保存到本机，确认无误再同步到服务器</p></div>
<label>正在设置 <select id="bot-select" onchange="BOT=this.value;renderFields()"></select></label></div>
<div class="config-tabs">
<button class="on" onclick="configTab(0,this)">规矩</button>
<button onclick="configTab(1,this)">记忆</button>
<button onclick="configTab(2,this)">认知</button>
<button onclick="configTab(3,this)">表达</button>
<button onclick="configTab(4,this)">运行</button>
</div>
<div class="config-section on" data-tab="规矩"></div>
<div class="config-section" data-tab="记忆"></div>
<div class="config-section" data-tab="认知"></div>
<div class="config-section" data-tab="表达"></div>
<div class="config-section" data-tab="运行"></div>
<div class="config-actions">
<button class="primary" onclick="configSync('push')">同步服务器</button>
<button onclick="configSync('pull')">重新读取服务器</button>
<span class="msg" id="managed-msg"></span>
</div>
<p class="hint">{restartmsg}</p>
<details class="advanced"><summary>高级：本机连接与图库配置</summary>
<textarea id="cfg">{config}</textarea>
<button class="btn" onclick="saveCfg()">保存本机配置</button>
<span class="msg" id="cmsg"></span></details>
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

<div class="panel">
<div class="bar"><button onclick="observePull('memory')">从服务器拉取记忆</button><span class="msg" id="memory-msg"></span></div>
<select class="observe-select" id="memory-files" aria-label="记忆文件" onchange="observeRead('memory')"></select>
<div class="observe-meta" id="memory-meta"></div>
<pre class="observe-text" id="memory-text"></pre>
</div>

<div class="panel">
<div class="bar"><button onclick="observePull('logs')">从服务器拉取日志</button><span class="msg" id="logs-msg"></span></div>
<select class="observe-select" id="logs-files" aria-label="日志文件" onchange="observeRead('logs')"></select>
<div class="observe-meta" id="logs-meta"></div>
<pre class="observe-text" id="logs-text"></pre>
</div>

<div class="panel">
<div class="bar">
<button onclick="pruneSeen()">清理墓碑</button>
<span class="msg" id="kmsg"></span>
</div>
<p class="hint">bot 靠这张表判断「这张图处理过了」。共 <b>{seencount}</b> 条，
其中 <b>{stale}</b> 条已不在图库（<b>墓碑</b>）—— 它们会让那张图再发也收不进来。</p>
<table class="seen">{seenrows}</table>
</div>

<div class="panel">
<div class="bar"><label>文字通道 <select id="channel-select" onchange="loadChannels()"></select></label>
<button onclick="loadChannels()">核对服务器</button><span class="msg" id="channels-msg"></span></div>
<table class="channels"><thead><tr><th>通道</th><th>格式</th><th>句号</th><th>日记</th><th>摘要</th><th>档案</th><th>图库</th><th>识图</th><th>群缓冲</th><th>人格绑定</th></tr></thead>
<tbody id="channel-rows"></tbody></table>
</div>

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
    items_js = json.dumps(items, ensure_ascii=False).replace("<", "\\u003c")

    def figure(it):
        fn = str(it.get("file") or "")
        cat = str(it.get("category") or "")
        tags = "".join('<span class="tag">%s</span>'
                       % html.escape(str(t)) for t in (it.get("tags") or [])[:5])
        esc_cat = html.escape(cat, quote=True)
        esc_fn = html.escape(fn, quote=True)
        n = item_index.get((cat, fn), -1)
        return (
            '<figure data-i="%d" data-cat="%s" data-file="%s">'
            '<img loading="lazy" src="/img/%s">'
            '<figcaption><b class="cname">%s</b>'
            '<div style="font-size:10px;color:#5d5768">%s</div>'
            '<span class="ctags">%s</span>'
            '<p class="cdesc" style="margin:4px 0 0;font-size:11px">%s</p>'
            '<div class="row"><button onclick="editItem(this.closest(\'figure\').dataset.cat,this.closest(\'figure\').dataset.file)">编辑</button>'
            '<button onclick="removeItem(this.closest(\'figure\').dataset.cat,this.closest(\'figure\').dataset.file)">移除</button></div>'
            '</figcaption></figure>' % (
                n, esc_cat, esc_fn, urllib.parse.quote(fn), html.escape(str(it.get("name") or "?")),
                html.escape(cat), tags, html.escape(str(it.get("desc") or "")),
                ))

    # 每个分类一个标题 + 一个网格：不同 bot 的图库一眼分得开
    blocks = []
    for cat in sorted(by_cat):
        blocks.append(
            '<h2 class="cat">%s<span>%d 张</span></h2><div class="grid">%s</div>'
            % (html.escape(cat or "(未分类)"), len(by_cat[cat]),
               "".join(figure(it) for it in by_cat[cat])))
    gallery = "".join(blocks) or '<p style="color:#6f6880">图库还是空的</p>'
    gcount = sum(len(v) for v in by_cat.values())


    sync_ok = bool(edit) and edit.sync_available()
    syncmsg = "" if sync_ok else "（未配置 ui.sync，同步功能不可用）"
    sync_settings = ((cfg.section("ui") or {}).get("sync") or {}) if cfg else {}
    restartmsg = ("同步后会重启 bot；约 30 秒无法收消息。服务器配置与唤醒设置会分别备份。"
                  if sync_settings.get("restart_command") else
                  "同步后需手动重启 bot 才会生效。服务器配置与唤醒设置会分别备份。")
    srows = seen_rows()
    stale_n = sum(1 for r in srows if not r["live"])
    seenrows = "".join(
        '<tr%s data-k="%s"><td><code>%s</code></td><td>%s</td>'
        '<td style="text-align:right"><button onclick="rmSeen(this)">移除</button></td></tr>'
        % ("" if r["live"] else ' class="tomb"', html.escape(r["key"], quote=True),
           html.escape(r["cat"]), html.escape(r["label"])) for r in srows
    ) or '<tr><td colspan="3" style="color:#6f6880">去重表是空的</td></tr>'
    page = PAGE.format(config=html.escape(raw), gallery=gallery,
                       token=TOKEN, syncmsg=html.escape(syncmsg),
                       restartmsg=html.escape(restartmsg),
                       gdir=html.escape(stickers_dir()), gcount=gcount,
                       seencount=len(srows), stale=stale_n, seenrows=seenrows)
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

        if path.startswith("/api/managed/"):
            if not managed:
                self._json({"ok": False, "error": "配置编辑模块不可用（需要 PyYAML）"})
                return
            try:
                action = path.rsplit("/", 1)[-1]
                if action == "view":
                    self._json({"ok": True, "data": managed.snapshot()})
                elif action == "change":
                    d = json.loads(body or "{}")
                    dirty = managed.change(d.get("source"), d.get("path"),
                                           str(d.get("bot") or ""), d.get("value"))
                    self._json({"ok": True, "dirty": dirty})
                elif action == "pull":
                    managed.pull()
                    self._json({"ok": True, "message": "已读取服务器配置"})
                elif action == "push":
                    self._json({"ok": True, "message": managed.push()})
                else:
                    self._json({"ok": False, "error": "未知操作"}, 404)
            except Exception as e:
                self._json({"ok": False, "error": str(e)[:180]})
            return

        if path.startswith("/api/observe/"):
            if not observe:
                self._json({"ok": False, "error": "观测模块不可用"})
                return
            try:
                parts = path.split("/")
                if len(parts) != 5:
                    raise ValueError("未知接口")
                kind, action = parts[3:]
                if action == "pull":
                    self._json({"ok": True, **observe.pull(kind)})
                elif action == "list":
                    self._json({"ok": True, "files": observe.listing(kind)})
                elif action == "read":
                    self._json({"ok": True, **observe.preview(kind, json.loads(body or "{}").get("id"))})
                else:
                    self._json({"ok": False, "error": "未知操作"}, 404)
            except Exception as e:
                self._json({"ok": False, "error": str(e)[:180]})
            return

        if path == "/api/channels/check":
            if not live:
                self._json({"ok": False, "error": "通道检查模块不可用"})
                return
            try:
                selected = json.loads(body or "{}").get("text_channel") or ""
                if not isinstance(selected, str):
                    raise ValueError("通道编号不合法")
                self._json({"ok": True, **live.channels_report(selected)})
            except Exception as e:
                self._json({"ok": False, "error": str(e)[:180]})
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

        if path == "/api/seen/remove":
            try:
                d = json.loads(body or "{}")
            except Exception:
                self._json({"ok": False, "error": "参数不是合法 JSON"})
                return
            k = str(d.get("key") or "")
            keys = load_seen()
            if k not in keys:
                self._json({"ok": False, "error": "去重表里没有这条"})
                return
            save_seen([x for x in keys if x != k])
            self._json({"ok": True, "message": "已解除"})
            return

        if path == "/api/seen/prune":
            dead = [r["key"] for r in seen_rows() if not r["live"]]
            keys = load_seen()
            save_seen([x for x in keys if x not in set(dead)])
            self._json({"ok": True, "message": "清掉 %d 条墓碑" % len(dead)})
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
