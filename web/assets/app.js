/* Lumen 桌面端前端 */
(() => {
'use strict';

const BOOT = JSON.parse(document.getElementById('lumen-boot').textContent || '{}');
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* 思考动效标记：原创「星核」——呼吸的星球 + 两条反向旋转的轨道环 + 环绕的卫星 */
const THINK_MARKUP = `<span class="lumen-think"><span class="lt-core">`
  + `<i class="lt-ring a"></i><i class="lt-ring b"></i>`
  + `<i class="lt-star"></i><i class="lt-sat"></i></span>`
  + `<span class="lt-txt">思考中</span></span>`;

const S = {
  sessions: [], cur: null, messages: [], settings: {}, tiers: [], hw: {}, engine: {},
  account: {}, devices: [], lan: {}, streaming: false, ctrl: null,
  remote: {}, drawer: null, tab: 'settings', modal: null, approvalsShown: new Set(),
  model: 'h1', seenSeq: 0, pendingSources: null,
};

/* ------------------------------------------------------------------ api */
async function api(path, body, opts = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (BOOT.token) headers['Authorization'] = 'Bearer ' + BOOT.token;
  const res = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const ct = res.headers.get('Content-Type') || '';
  const data = ct.includes('json') ? await res.json() : await res.text();
  if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
  return data;
}

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('#toastRoot').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateY(6px)'; el.style.transition = '.25s'; }, 2600);
  setTimeout(() => el.remove(), 3000);
}

/* ------------------------------------------------------------- markdown */
function md(src, keepCite) {
  if (!src) return '';
  const blocks = [];
  let text = String(src).replace(/\r\n/g, '\n');
  text = text.replace(/```([\w+#.-]*)\n([\s\S]*?)```/g, (m, lang, code) => {
    const i = blocks.push({ lang: lang || 'text', code: code.replace(/\n$/, '') }) - 1;
    return `\u0000B${i}\u0000`;
  });

  const inline = (s) => {
    s = esc(s);
    s = s.replace(/`([^`\n]+)`/g, (m, c) => `<code>${c}</code>`);
    s = s.replace(/\*\*\*([^*\n]+)\*\*\*/g, '<strong><em>$1</em></strong>');
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
    s = s.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
    s = s.replace(/\[([^\]\n]{1,80})\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)）]+)/g, '$1<a href="$2" target="_blank" rel="noreferrer">$2</a>');
    // 没有引用来源时，把正文里孤立的 [1] 标记去掉——挂着没有对应的来源卡片，
    // 读者只看到一堆方括号，很不专业。
    if (keepCite === false) {
      s = s.replace(/\s*\[(\d{1,2})\]/g, '');
    } else {
      s = s.replace(/\[(\d{1,2})\]/g, '<sup class="cite">[$1]</sup>');
    }
    return s;
  };

  const lines = text.split('\n');
  let out = [], i = 0, listType = null, para = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join(' '))}</p>`); para = []; } };
  const closeList = () => { if (listType) { out.push(`</${listType}>`); listType = null; } };

  while (i < lines.length) {
    const raw = lines[i];
    const line = raw.trim();
    if (!line) { flushPara(); closeList(); i++; continue; }

    if (/^\u0000B\d+\u0000$/.test(line)) { flushPara(); closeList(); out.push(line); i++; continue; }

    let m = line.match(/^(#{1,6})\s+(.*)$/);
    if (m) { flushPara(); closeList(); const lv = Math.min(3, m[1].length); out.push(`<h${lv}>${inline(m[2])}</h${lv}>`); i++; continue; }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) { flushPara(); closeList(); out.push('<hr>'); i++; continue; }

    if (/^>\s?/.test(line)) {
      flushPara(); closeList();
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i].trim())) { buf.push(lines[i].trim().replace(/^>\s?/, '')); i++; }
      out.push(`<blockquote>${md(buf.join('\n'))}</blockquote>`);
      continue;
    }

    if (/^\|.*\|$/.test(line) && i + 1 < lines.length && /^\|[\s:|-]+\|$/.test(lines[i + 1].trim())) {
      flushPara(); closeList();
      const head = line.split('|').slice(1, -1).map((c) => c.trim());
      i += 2;
      const rows = [];
      while (i < lines.length && /^\|.*\|$/.test(lines[i].trim())) {
        rows.push(lines[i].trim().split('|').slice(1, -1).map((c) => c.trim()));
        i++;
      }
      out.push('<table><thead><tr>' + head.map((h) => `<th>${inline(h)}</th>`).join('') + '</tr></thead><tbody>' +
        rows.map((r) => '<tr>' + r.map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>').join('') + '</tbody></table>');
      continue;
    }

    m = line.match(/^([-*+]|\d+[.)])\s+(.*)$/);
    if (m) {
      flushPara();
      const type = /\d/.test(m[1]) ? 'ol' : 'ul';
      if (listType !== type) { closeList(); out.push(`<${type}>`); listType = type; }
      out.push(`<li>${inline(m[2])}</li>`);
      i++; continue;
    }

    closeList();
    para.push(line);
    i++;
  }
  flushPara(); closeList();

  let html = out.join('\n');
  html = html.replace(/\u0000B(\d+)\u0000/g, (m, idx) => {
    const b = blocks[+idx];
    if (!b) return '';
    return `<div class="code-wrap"><div class="code-head"><span>${esc(b.lang)}</span>
      <button class="cp" data-copy="${+idx}">复制</button></div><pre><code>${esc(b.code)}</code></pre></div>`;
  });
  return html;
}

/* --------------------------------------------------------------- render */
function tierById(id) { return S.tiers.find((t) => t.id === id) || S.tiers[0] || {}; }

/* 界面上一律显示档位名，不显示权重的原始文件名。
   例如 H1-Turbo-Q4_K_M.gguf → Lumen H1 Turbo。
   用户自己放进 models/ 的权重保留原名（那是用户自己的文件）。 */
/* 友好名 → 真实权重文件名。用户看到的是档位名，发给后端的还得是 .gguf。 */
function realModelName(name) {
  const v = String(name || '').trim();
  if (!v) return v;
  // 当前引擎正在跑的就是这个档位 → 用真正在跑的那个文件名
  const engModel = (S.engine && S.engine.model) || '';
  if (engModel && friendlyModel(engModel) === v) return engModel;
  const t = (S.tiers || []).find((x) => x.name === v);
  if (t) return t.model_file || (t.model_files || [])[0] || v;
  return v;
}

function friendlyModel(name) {
  if (!name) return '';
  const n = String(name).trim();
  for (const t of (S.tiers || [])) {
    const files = t.model_files || [];
    if (files.includes(n)) return t.name;
  }
  // 兜底：按去掉扩展名后的前缀匹配（模型列表接口可能返回带路径的完整文件名）
  const base = n.split(/[\\/]/).pop();
  for (const t of (S.tiers || [])) {
    for (const f of (t.model_files || [])) {
      if (f === base) return t.name;
    }
  }
  return n;
}

function renderSidebar() {
  const box = $('#sessionList');
  if (!S.sessions.length) { box.innerHTML = '<div class="session-empty">还没有对话</div>'; return; }
  box.innerHTML = S.sessions.map((s) => `
    <div class="sess ${s.id === S.cur ? 'on' : ''}" data-sid="${s.id}">
      <span class="sess-t">${esc(s.title || '新对话')}</span>
      <span class="sess-n">${s.msg_count || 0}</span>
      <button class="sess-del" data-del="${s.id}" title="删除">
        <svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/></svg>
      </button>
    </div>`).join('');
}

function renderTopbar() {
  const t = tierById(S.model);
  $('#mpName').textContent = t.name || 'Lumen';
  const ctx = (S.settings.engine && S.settings.engine.num_ctx) || 8192;
  $('#mpMeta').textContent = `${t.quant || ''} · ${Math.round(ctx / 1024)}K`.replace(/^ · /, '');
  const off = S.engine && S.engine.fallback;
  $('#mpDot').className = 'mp-dot' + (off ? ' off' : '');
  const engTag = $('#engTag');
  if (!S.engine || S.engine.fallback) { engTag.className = 'tag warn'; engTag.textContent = '离线规则引擎'; }
  else {
    const lc = S.engine.llamacpp || {};
    const backend = S.engine.backend;
    let label = '本地引擎在线';
    if (backend === 'llamacpp') label = lc.backend === 'vulkan' ? 'Vulkan GPU 加速' : (lc.backend === 'cpu' ? '本地引擎 · CPU' : '本地引擎在线');
    else if (backend === 'onnx') label = '本地引擎 · ONNX';
    engTag.className = 'tag';
    engTag.textContent = label;
    engTag.title = `${friendlyModel(S.engine.model) || ''} · ${S.engine.base_url || ''}`;
  }
  $('#agentTag').className = 'tag ghost' + (agentEnabled() ? '' : ' dim');
  $('#agentTag').textContent = agentEnabled() ? 'Agent 已启用' : 'Agent 关闭';
  $('#hwMini').innerHTML = `<b>${esc(S.hw.gpu_name || '未检测到 GPU')}</b><br>${S.hw.vram_gb || 0}GB 显存 · ${(S.hw.memory || {}).total_gb || 0}GB 内存`;
  $('#ctxHint').textContent = `${Math.round(ctx / 1024)}K 上下文`;
  $('#brandSub').textContent = t.short ? `${t.short} · 本地` : '本地 AI 工作台';
}

function renderWelcome() {
  const t = tierById(S.model);
  $('#welcome').hidden = !!S.messages.length;
  $('#thread').hidden = !S.messages.length;
  if (S.messages.length) return;
  const cards = [
    ['解释这个仓库的结构', '让我读一遍当前工作区，画出模块关系'],
    ['帮我修一个 Bug', '告诉我现象，我会读文件、定位、改代码'],
    ['写个脚本批量重命名', '我会生成脚本并在本机执行，执行前先给你确认'],
    ['今天有什么值得关注的事', '低置信度会自动联网检索，并给出引用来源'],
  ];
  $('#promptCards').innerHTML = cards.map(([a, b]) =>
    `<div class="pcard" data-p="${esc(a)}"><b>${esc(a)}</b><span>${esc(b)}</span></div>`).join('');
  const hw = S.hw || {};
  $('#welcomeHw').textContent = `${hw.gpu_name || ''} · ${hw.vram_gb || 0}GB VRAM · ${t.name || ''}`;
}

function msgHTML(m, idx) {
  if (m.role === 'user') {
    return `<div class="msg me" data-i="${idx}">
      <div class="avatar">你</div>
      <div class="bubble"><div class="body">${md(m.content, false)}</div></div>
    </div>`;
  }
  const meta = m.meta || {};
  const tools = (meta.tools || []).map(toolCardHTML).join('');
  const srcs = (meta.sources || []).length ? sourcesHTML(meta.sources, meta.provider) : '';
  let badges = '';
  if (meta.confidence != null) {
    const low = meta.confidence < 60;
    badges += `<span class="badge ${low ? 'low' : 'good'}">置信度 ${meta.confidence}</span>`;
  }
  if (meta.engine === 'offline') badges += `<span class="badge">离线规则引擎</span>`;
  const reason = (meta.reasoning || '')
    ? `<div class="reason-box"><div class="reason-head"><i class="reason-caret"></i>思考过程</div><div class="reason-text">${esc(meta.reasoning)}</div></div>`
    : '';
  return `<div class="msg ai" data-i="${idx}">
    <div class="avatar"><img src="/assets/logo-round.png" alt=""></div>
    <div class="bubble">
      ${reason}
      <div class="body">${md(m.content, (meta.sources || []).length > 0)}</div>
      ${tools}
      ${srcs}
      <div class="meta-row">${badges}
        <div class="msg-actions">
          <button data-copy-msg="${idx}" title="复制"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 012-2h8"/></svg></button>
          <button data-regen="${idx}" title="重新生成"><svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 0115-6.7L21 8M21 21v-5h-5M21 12a9 9 0 01-15 6.7L3 16"/></svg></button>
        </div>
      </div>
    </div>
  </div>`;
}

function toolCardHTML(t) {
  const icon = { run_cmd: 'M4 17l5-5-5-5M12 19h8', fs_read: 'M7 4h7l4 4v12H7z', fs_write: 'M12 5v14M5 12h14', search: 'M11 4a7 7 0 100 14 7 7 0 000-14zM20 20l-3.6-3.6', fs_ls: 'M4 7h6l2 2h8v9H4z', fetch: 'M12 3a9 9 0 100 18 9 9 0 000-18zM3 12h18M12 3c3 3.5 3 14 0 18', fs_append: 'M12 5v14M5 12h14' }[t.name] || 'M12 5v14';
  const st = t.status || 'ok';
  const sttxt = st === 'running' ? '执行中' : st === 'error' ? '失败' : '完成';
  return `<div class="tool-card" data-tool="${esc(t.id || '')}">
    <div class="tool-head">
      <span class="tico"><svg viewBox="0 0 24 24"><path d="${icon}"/></svg></span>
      <span class="tool-name">${esc(t.label || t.name)}</span>
      <span class="tool-args">${esc(JSON.stringify(t.args || {}).slice(0, 120))}</span>
      <span class="tool-status ${st}">${sttxt}</span>
    </div>
    ${t.result ? `<div class="tool-out">${esc(t.result)}</div>` : ''}
  </div>`;
}

function sourcesHTML(list, provider) {
  return `<div class="sources">
    <div class="sources-h">引用来源${provider ? ' · ' + esc(provider) : ''}</div>
    ${list.map((s) => `<a class="src" href="${esc(s.url)}" target="_blank" rel="noreferrer">
      <span class="src-i">${s.index}</span>
      <span class="src-t"><b>${esc(s.title || s.url)}</b><span>${esc((s.snippet || s.url).slice(0, 90))}</span></span>
    </a>`).join('')}
  </div>`;
}

function renderThread() {
  const box = $('#thread');
  box.innerHTML = S.messages.map((m, i) => msgHTML(m, i)).join('');
  $('#welcome').hidden = !!S.messages.length;
  box.hidden = !S.messages.length;
  // 历史消息里的「思考过程」默认折叠，点标题展开
  $$('#thread .reason-head').forEach((h) => h.addEventListener('click', () => {
    const box2 = h.parentElement;
    if (box2) box2.classList.toggle('open');
  }));
  scrollBottom(true);
}

function scrollBottom(force) {
  const st = $('#stage');
  if (!st) return;
  const near = st.scrollHeight - st.scrollTop - st.clientHeight < 220;
  if (force || near) st.scrollTop = st.scrollHeight;
}

/* ------------------------------------------------------------ 会话操作 */
async function loadSessions() {
  const r = await api('/api/sessions');
  S.sessions = r.sessions || [];
  renderSidebar();
}

async function openSession(id) {
  const s = await api('/api/session?id=' + encodeURIComponent(id));
  S.cur = s.id;
  S.messages = (s.messages || []).map((m) => ({ role: m.role, content: m.content, meta: m.meta || {}, ts: m.ts }));
  renderSidebar();
  renderThread();
  closeDrawer();
}

function newChat() {
  S.cur = null;
  S.messages = [];
  renderSidebar();
  renderThread();
  $('#input').focus();
}

/* ----------------------------------------------------------------- 发送 */
function agentEnabled() {
  const t = tierById(S.model);
  return !!(S.settings.agent && S.settings.agent.enabled) && (t.capabilities || []).includes('agent');
}

async function send(text) {
  if (S.streaming || !text.trim()) return;
  const clean = text.trim();
  $('#input').value = '';
  autoGrow();
  if (!S.cur) {
    const r = await api('/api/sessions/new', { title: clean.slice(0, 24) || '新对话' });
    S.cur = r.session.id;
    S.messages = [];
    loadSessions();
  }
  S.messages.push({ role: 'user', content: clean, meta: {} });
  const ai = { role: 'assistant', content: '', meta: {} };
  S.messages.push(ai);
  renderThread();

  const idx = S.messages.length - 1;
  const host = $(`.msg.ai[data-i="${idx}"] .bubble`);
  host.innerHTML = `<div class="body"><span class="typing"><i></i><i></i><i></i></span></div>`;
  host.insertAdjacentHTML('beforeend', '<div class="meta-row"></div>');

  S.streaming = true;
  setSendState(true);
  const ctrl = new AbortController();
  S.ctrl = ctrl;

  let buf = '', statusEl = null, sources = [], tools = [], confidence = null, engineKind = 'model', dirty = false;
  let reasonEl = null, reasonBuf = '';
  const paint = () => {
    if (!dirty) return;
    dirty = false;
    const bodyEl = host.querySelector('.body');
    if (bodyEl) bodyEl.innerHTML = md(buf, sources.length > 0) + (S.streaming ? '<span class="caret"></span>' : '');
    scrollBottom();
  };

  try {
    const headers = { 'Content-Type': 'application/json' };
    if (BOOT.token) headers['Authorization'] = 'Bearer ' + BOOT.token;
    const res = await fetch('/api/chat', {
      method: 'POST', headers,
      body: JSON.stringify({ session_id: S.cur, message: clean }),
      signal: ctrl.signal,
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let acc = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      acc += dec.decode(value, { stream: true });
      let cut;
      while ((cut = acc.indexOf('\n\n')) >= 0) {
        const chunk = acc.slice(0, cut); acc = acc.slice(cut + 2);
        const line = chunk.split('\n').find((l) => l.startsWith('data: '));
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch { continue; }
        handleEvent(ev);
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') buf += `\n\n> ⚠ 连接中断：${e.message}`;
  } finally {
    S.streaming = false;
    S.ctrl = null;
    setSendState(false);
    paint();
    ai.content = buf;
    ai.meta = { sources, tools, confidence, engine: engineKind, reasoning: reasonBuf };
    renderThread();
    loadSessions();
  }

  function handleEvent(ev) {
    switch (ev.type) {
      case 'meta':
        S.engine = ev.engine || S.engine;
        renderTopbar();
        break;
      case 'status': {
        if (!statusEl) {
          statusEl = document.createElement('div');
          statusEl.className = 'status-line';
          host.insertBefore(statusEl, host.querySelector('.meta-row'));
        }
        statusEl.innerHTML = `<span class="spin"></span><span>${esc(ev.text)}</span>`;
        scrollBottom();
        break;
      }
      case 'reset':
        buf = '';
        if (statusEl) statusEl.remove();
        statusEl = null;
        if (reasonEl) reasonEl.remove();
        reasonEl = null;
        reasonBuf = '';
        dirty = true; paint();
        break;
      case 'token':
        buf += ev.text;
        dirty = true;
        if (!paint.queued) {
          paint.queued = true;
          requestAnimationFrame(() => { paint.queued = false; paint(); });
        }
        break;
      case 'thinking': {
        // 思考动效：开始时显示可动的小图标，结束时收起并把思考折进折叠区
        if (ev.state === 'start') {
          if (!reasonEl) {
            reasonEl = document.createElement('div');
            reasonEl.className = 'reason-box thinking';
            const bodyEl = host.querySelector('.body');
            if (bodyEl) host.insertBefore(reasonEl, bodyEl);
            else host.insertBefore(reasonEl, host.querySelector('.meta-row'));
          }
          reasonEl.innerHTML = THINK_MARKUP;
        } else {
          const txt = (ev.text || '').trim();
          reasonBuf = txt;
          if (reasonEl) {
            if (txt) {
              reasonEl.className = 'reason-box';
              reasonEl.innerHTML = `<div class="reason-head"><i class="reason-caret"></i>思考过程</div>`
                + `<div class="reason-text">${esc(txt)}</div>`;
              const head = reasonEl.querySelector('.reason-head');
              if (head) head.addEventListener('click', () => reasonEl.classList.toggle('open'));
            } else {
              reasonEl.remove();
              reasonEl = null;
            }
          }
        }
        scrollBottom();
        break;
      }
      case 'sources':
        sources = ev.sources || [];
        break;
      case 'tool': {
        const ex = tools.find((t) => t.id === ev.id);
        if (ex) Object.assign(ex, ev);
        else tools.push(ev);
        let row = host.querySelector('.tool-row');
        if (!row) {
          row = document.createElement('div');
          row.className = 'tool-row';
          host.insertBefore(row, host.querySelector('.meta-row'));
        }
        row.innerHTML = tools.map(toolCardHTML).join('');
        scrollBottom();
        break;
      }
      case 'error':
        buf += `\n\n> ⚠ ${ev.text}`;
        dirty = true; paint();
        break;
      case 'done':
        confidence = ev.confidence != null ? ev.confidence : null;
        engineKind = ev.engine || 'model';
        if (ev.sources && ev.sources.length) sources = ev.sources;
        if (ev.tools) tools = ev.tools.map((t) => tools.find((x) => x.id === t.id) || t);
        if (statusEl) { statusEl.remove(); statusEl = null; }
        break;
      default: break;
    }
  }
}

function setSendState(running) {
  const b = $('#btnSend');
  b.classList.toggle('stop', running);
  b.innerHTML = running
    ? '<svg viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="2"/></svg>'
    : '<svg viewBox="0 0 24 24"><path d="M4 12l16-8-6 8 6 8z"/></svg>';
  b.title = running ? '停止' : '发送';
}

/* ----------------------------------------------------------------- 模型 */
async function openModelPicker() {
  const r = await api('/api/tiers');
  S.tiers = r.tiers; S.hw = r.hardware;
  const body = r.tiers.map((t) => {
    const on = t.id === S.model;
    const srcLine = t.show_source && t.source
      ? `<div class="tier-src">底层来源：<b>${esc(t.source)}</b> · ${esc(t.credit || '')}</div>`
      : `<div class="tier-src">${esc(t.credit || '')} · 第三方许可证声明见 <b>licenses/${esc((t.notice || '').split('/').pop())}</b></div>`;
    return `<div class="tier ${on ? 'on' : ''} ${t.available ? '' : 'locked'}" data-tier="${t.id}">
      <div class="tier-ic">${t.tier_index === 3 ? 'V1' : t.tier_index === 2 ? 'H1' : 'SE'}</div>
      <div class="tier-main">
        <div class="tier-top">
          <span class="tier-name">${esc(t.name)}</span>
          <span class="tier-badge gold">${esc(t.badge)}</span>
          ${t.available ? `<span class="tier-badge ok">本机可用</span>` : `<span class="tier-badge lock">显存不足</span>`}
          ${t.model_present
            ? `<span class="tier-badge ok">模型已就位</span>`
            : `<span class="tier-badge">${esc(t.model_present_label)}</span>
               <button class="btn sm" data-getweight="1" style="margin-left:6px">获取权重</button>`}
        </div>
        <div class="tier-desc">${esc((t.strengths || []).join(' · '))}</div>
        <div class="tier-specs">
          <span class="spec">${esc(t.params)}</span>
          <span class="spec">${esc(t.quant)}</span>
          <span class="spec">${Math.round(t.ctx / 1024)}K ctx</span>
          <span class="spec">磁盘 ${t.disk_gb}GB</span>
          <span class="spec">显存 ${esc(t.vram_use)}</span>
          <span class="spec">≥${t.min_vram_gb}GB</span>
        </div>
        ${t.available ? '' : `<div class="tier-lockmsg">🔒 ${esc(t.gate_reason)}</div>`}
        ${srcLine}
      </div>
    </div>`;
  }).join('');

    openModal({
    title: '选择模型档位',
    sub: `${S.hw.gpu_name} · ${S.hw.vram_gb}GB 显存`,
    body: `<div>${body}</div>
      <div class="note" style="margin-top:6px"><b>两种「不可用」不是一回事：</b><br>
      · <b>卡片变灰</b>＝本机显存不够跑这一档，属于硬件门槛，可在「设置 → 实验性选项」强制加载；<br>
      · <b>灰度但标着「待放入」</b>＝硬件够、只是权重文件还没装，点「获取权重」一键下载即可，装完这一档立刻真正可用。</div>`,
    wide: true,
  });

  $$('.tier').forEach((el) => el.addEventListener('click', async () => {
    const id = el.dataset.tier;
    const t = tierById(id);
    if (!t.available) { toast(t.gate_reason, 'err'); return; }
    try {
      const res = await api('/api/tier', { tier: id });
      S.model = id;
      S.settings = res.settings;
      S.engine = res.engine;
      closeModal();
      renderTopbar();
      renderWelcome();
      toast(`已切换到 ${t.name}`, 'ok');
    } catch (e) { toast(e.message, 'err'); }
  }));

  $$('[data-getweight]').forEach((el) => el.addEventListener('click', (e) => {
    e.stopPropagation();
    openWeightsModal();
  }));
}

/* ------------------------------------------------------- 权重仓库（下载） */
function weightsBody(r) {
  const p = (r.progress || {});
  const done = p.state === 'done' && p.id;
  const rows = (r.catalog || []).map((w) => {
    const running = p.running && p.id === w.id;
    const bar = running
      ? `<div class="dlbar"><i style="width:${Math.max(2, Math.round(p.percent || 0))}%"></i></div>
         <div class="note" style="font-size:11px">${esc(p.text || '下载中…')}</div>`
      : '';
    const btn = w.installed
      ? `<span class="pill good">已安装 ${w.local_gb}GB</span>
         <button class="btn sm danger" data-wremove="${esc(w.id)}" style="margin-left:6px">删除</button>`
      : `<button class="btn sm primary" data-wpull="${esc(w.id)}">下载 ${w.size_gb}GB</button>`;
    return `<div class="card" data-wcard="${esc(w.id)}">
      <div class="kv"><span><b>${esc(w.title)}</b></span>${btn}</div>
      <div class="note">${esc(w.note || '')}</div>
      <div class="note" style="font-size:11px">上游 ${esc(w.upstream)} · 许可证 ${esc(w.license)} · 文件 ${esc(w.file)}</div>
      ${bar}
    </div>`;
  }).join('');
  const doneNote = (done && !p.running)
    ? `<div class="note warn" style="margin-bottom:8px">✅ 权重已下载完成。现在到<b>「模型档位」</b>里选择对应档位即可真正启用；
       切换档位会自动用这个权重重启本地运行时。</div>`
    : '';
  return `<div class="note" style="margin-bottom:8px">权重直接存进 <b>models/</b>（${esc(r.model_dir || '')}），
    当前可用空间 ${r.free_gb}GB。下载走公开镜像与官方仓库，带断点续传；每个候选都标注了上游与许可证。</div>
    ${doneNote}${rows}
    <div class="note" style="margin-top:6px">已经下载好的权重也可以自己放进 models/ 目录，Lumen 会自动识别。</div>`;
}

async function openWeightsModal() {
  try {
    const r = await api('/api/weights');
    S.weights = r;
    openModal({
      title: '权重仓库',
      sub: `可用空间 ${r.free_gb}GB`,
      body: weightsBody(r),
      wide: true,
    });
    bindWeights();
    if ((r.progress || {}).running) startWeightsPoll();
  } catch (e) { toast(e.message, 'err'); }
}

let _wPoll = null;
function startWeightsPoll() {
  if (_wPoll) clearInterval(_wPoll);
  _wPoll = setInterval(async () => {
    try {
      const r = await api('/api/weights');
      S.weights = r;
      const body = document.querySelector('#modalRoot .modal-b');
      if (!body) { stopWeightsPoll(); return; }
      body.innerHTML = weightsBody(r);
      bindWeights();
      if (!(r.progress || {}).running) stopWeightsPoll();
    } catch (e) { stopWeightsPoll(); }
  }, 1200);
}
function stopWeightsPoll() { if (_wPoll) { clearInterval(_wPoll); _wPoll = null; } }

function bindWeights() {
  $$('[data-wpull]').forEach((el) => el.addEventListener('click', async () => {
    const id = el.dataset.wpull;
    el.disabled = true;
    try {
      const r = await api('/api/weights/pull', { id });
      toast(r.message || '已开始下载', 'ok');
      startWeightsPoll();
    } catch (e) { toast(e.message, 'err'); el.disabled = false; }
  }));
  $$('[data-wremove]').forEach((el) => el.addEventListener('click', async () => {
    const id = el.dataset.wremove;
    if (!confirm('确认删除这个权重文件？删除后可重新下载。')) return;
    try {
      const r = await api('/api/weights/remove', { id });
      toast(r.message || '已删除', 'ok');
      const rr = await api('/api/weights');
      S.weights = rr;
      const body = document.querySelector('#modalRoot .modal-b');
      if (body) { body.innerHTML = weightsBody(rr); bindWeights(); }
    } catch (e) { toast(e.message, 'err'); }
  }));
}

/* ----------------------------------------------------------------- 抽屉 */
function openDrawer(tab) {
  S.drawer = tab || 'settings';
  $('#drawer').hidden = false;
  $$('.dtab').forEach((b) => b.classList.toggle('on', b.dataset.tab === S.drawer));
  renderDrawer();
  // 打开手机面板时拉一次最新局域网信息（IP 可能已经变了，二维码要跟着更新）
  if (S.drawer === 'phone') {
    api('/api/lan').then((l) => {
      S.lan = l;
      if (S.drawer === 'phone') { renderDrawer(); }
    }).catch(() => {});
    loadFirewall();
  }
}

/* 防火墙体检：手机能不能连上，本质上取决于 Windows 放不放行 8770 */
async function loadFirewall() {
  const first = $('#fwCard');
  if (first) first.innerHTML = '<div class="note">正在检查防火墙状态…</div>';
  try {
    const f = await api('/api/firewall');
    S.firewall = f;
    // 请求在飞的过程中抽屉可能被重绘（局域网信息回包会 renderDrawer），
    // 所以这里要重新取一次节点，否则结果写进了已经被替换掉的旧节点。
    const card = $('#fwCard');
    if (!card) return;
    const rows = [
      `<div class="kv"><span>局域网端口 ${f.desktop_port}</span><b>${f.lan_allowed ? '已放行' : '未放行'}</b></div>`,
      `<div class="kv"><span>守护端口 ${f.guard_port}</span><b>${f.guard_allowed ? '已放行' : '未放行'}</b></div>`,
    ];
    if ((f.stale_blocks || []).length) {
      rows.push(`<div class="kv"><span>遗留拦截规则</span><b>${f.stale_blocks.length} 条</b></div>`);
    }
    card.innerHTML = rows.join('') + `<div class="note">${esc(f.message || '')}</div>`;
  } catch (e) {
    const card = $('#fwCard');
    if (card) card.innerHTML = `<div class="note">检查失败：${esc(e.message)}</div>`;
  }
}
function closeDrawer() { $('#drawer').hidden = true; S.drawer = null; }

function switchTab(tab) {
  S.drawer = tab;
  $$('.dtab').forEach((b) => b.classList.toggle('on', b.dataset.tab === tab));
  renderDrawer();
}

function renderDrawer() {
  const box = $('#drawerBody');
  if (S.drawer === 'settings') box.innerHTML = drawerSettings();
  else if (S.drawer === 'phone') box.innerHTML = drawerPhone();
  else if (S.drawer === 'guard') box.innerHTML = drawerGuard();
  else box.innerHTML = drawerAbout();
  bindDrawer();
  if (S.drawer === 'phone') { paintQr(); loadFirewall(); showRelayState(); }
}

async function showRelayState() {
  const el = $('#relayState');
  if (!el) return;
  el.textContent = '查询中…';
  try {
    const r = await api('/api/relay/status');
    const s = (r && r.relay) || {};
    if (s.running) {
      const up = s.since ? Math.max(0, Math.round((Date.now() / 1000) - s.since)) + 's' : '';
      el.innerHTML = '<span class="pill good">已连接中继</span> ' + esc(s.host || '') + (up ? ' · 已连 ' + up : '');
    } else {
      el.textContent = s.error ? ('未连接：' + s.error) : '中继未启用或未连接';
    }
  } catch (e) {
    el.textContent = '无法查询中继状态：' + e.message;
  }
}

/* 局域网二维码：手机相机扫一下就能直连，不用手输 IP */
function paintQr() {
  const canvas = $('#qrCanvas');
  const urlEl = $('#qrUrl');
  if (!canvas || !urlEl) return;
  const urls = (S.lan && S.lan.urls) || [];
  const url = urls[0] || '';
  urlEl.textContent = url || '未检测到局域网 IP';
  if (!url || typeof qrcode === 'undefined') {
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = 'rgba(255,255,255,.05)';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = 'rgba(255,255,255,.4)';
    ctx.font = '13px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('无局域网 IP', canvas.width / 2, canvas.height / 2);
    return;
  }
  try {
    const qr = qrcode(0, 'M');
    qr.addData(url);
    qr.make();
    const n = qr.getModuleCount();
    const size = canvas.width;
    const cell = Math.floor(size / n);
    const off = Math.floor((size - cell * n) / 2);
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, size, size);
    ctx.fillStyle = '#000000';
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) {
        if (qr.isDark(r, c)) ctx.fillRect(off + c * cell, off + r * cell, cell, cell);
      }
    }
  } catch (e) {
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = 'rgba(255,255,255,.4)';
    ctx.font = '13px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('二维码生成失败', canvas.width / 2, canvas.height / 2);
  }
}

function sw(on, key) { return `<div class="switch ${on ? 'on' : ''}" data-sw="${key}"></div>`; }

function drawerSettings() {
  const s = S.settings, se = s.search || {}, ag = s.agent || {}, en = s.engine || {};
  const force = !!s.force_tier;
  const lc = (S.engine && S.engine.llamacpp) || {};
  return `
  <div class="sec"><div class="sec-h">推理引擎</div>
    <div class="field"><label>端点地址<span class="hint">Ollama 11434 / llama.cpp 8080 / LM Studio 1234</span></label>
      <input type="text" id="f-base" value="${esc(en.base_url || '')}" style="width:170px"></div>
    <div class="field"><label>模型名<span class="hint">留空则自动匹配当前档位</span></label>
      <input type="text" id="f-model" value="${esc(friendlyModel(en.model))}" style="width:170px"
             title="填档位名（如 Lumen H1 Turbo）或权重文件名"></div>
    <div class="field"><label>上下文长度</label>
      <input type="number" id="f-ctx" value="${en.num_ctx || 8192}" step="1024" min="2048" max="262144"></div>
    <div class="field"><label>温度</label>
      <input type="number" id="f-temp" value="${en.temperature ?? 0.6}" step="0.05" min="0" max="2"></div>
    <div class="btn-row"><button class="btn sm" id="btnRedetect">重新探测端点</button>
      <button class="btn sm primary" id="btnSaveEngine">保存</button></div>
    <div class="card note" id="engInfo">${esc(S.engine && S.engine.fallback ? '当前未检测到本地模型端点，使用离线规则引擎。' : '已连接：' + (S.engine.base_url || '') + ' · ' + friendlyModel(S.engine.model))}</div>
  </div>

  <div class="sec"><div class="sec-h">内置推理运行时</div>
    <div class="kv"><span>状态</span><b>${lc.running ? (lc.backend === 'vulkan' ? '运行中 · Vulkan GPU' : (lc.backend === 'cpu' ? '运行中 · CPU' : '运行中')) : (lc.available ? '已就绪 · 未启动' : '运行时缺失')}</b></div>
    <div class="kv"><span>当前模型</span><b>${esc(friendlyModel(lc.model) || '—')}</b></div>
    <div class="kv"><span>可用权重</span><b>${esc((lc.models || []).map(friendlyModel).join('、') || 'models/ 下没有 .gguf')}</b></div>
    <div class="btn-row">
      <button class="btn sm" id="btnLcStart">启动</button>
      <button class="btn sm" id="btnLcStop">停止</button>
    </div>
    <div class="card note">Lumen 自带 llama.cpp 的 Vulkan 构建，把 .gguf 放进 <code>models/</code> 就能直接用显卡跑，不需要另外装 Ollama。</div>
  </div>

  <div class="sec"><div class="sec-h">智能搜索</div>
    <div class="field"><label>联网搜索<span class="hint">关闭后模型不得编造时事</span></label>${sw(se.enabled, 'search.enabled')}</div>
    <div class="field"><label>检索模式</label>
      <select id="f-mode">
        <option value="low_confidence" ${se.mode !== 'always' ? 'selected' : ''}>仅低置信度时</option>
        <option value="always" ${se.mode === 'always' ? 'selected' : ''}>始终搜索</option>
      </select></div>
    <div class="field"><label>置信度阈值</label>
      <input type="number" id="f-thr" value="${se.threshold ?? 60}" min="0" max="100"></div>
    <div class="field"><label>SearXNG 地址<span class="hint">留空则用必应 RSS 兜底</span></label>
      <input type="text" id="f-searx" value="${esc(se.searxng_url || '')}" placeholder="http://127.0.0.1:8888" style="width:170px"></div>
    <div class="btn-row"><button class="btn sm" id="btnTestSearch">试一次检索</button>
      <button class="btn sm primary" id="btnSaveSearch">保存</button></div>
  </div>

  <div class="sec"><div class="sec-h">Agent 工具</div>
    <div class="field"><label>允许工具调用<span class="hint">读写文件 / 执行命令 / 抓网页</span></label>${sw(ag.enabled, 'agent.enabled')}</div>
    <div class="field"><label>写文件需确认</label>${sw(ag.require_approval !== false, 'agent.require_approval')}</div>
    <div class="field"><label>允许执行命令</label>${sw(ag.allow_shell !== false, 'agent.allow_shell')}</div>
    <div class="field"><label>单轮最大步数</label>
      <input type="number" id="f-steps" value="${ag.max_steps || 6}" min="1" max="20"></div>
    <div class="field"><label>工作区目录</label><button class="btn sm" id="btnOpenWs">打开</button></div>
  </div>

  <div class="sec"><div class="sec-h">实验性选项</div>
    <div class="field"><label>强制加载高显存档位<span class="hint">显存不足时也可能爆卡，仅限折腾</span></label>${sw(force, 'force_tier')}</div>
  </div>

  <div class="sec"><div class="sec-h">账号</div>
    <div class="kv"><span>账号</span><b>${esc(S.account.username || '')}</b></div>
    <div class="kv"><span>本机设备</span><b>${esc(S.account.device_name || '')}</b></div>
    <div class="field"><label>${S.account.default_password ? '当前仍是默认密码，建议修改' : '修改密码'}</label>
      <input type="password" id="f-pwd" placeholder="新密码" style="width:140px">
      <button class="btn sm" id="btnSavePwd">保存</button></div>
    ${S.account.default_password ? '<div class="card note warn">默认账号 <b>' + esc(S.account.username) + '</b>，默认密码 <b>lumen</b>。手机端首次连接请用这组凭据，登录后请立刻改密码。</div>' : ''}
  </div>`;
}

function drawerPhone() {
  const lan = S.lan || {};
  const rm = S.remote || {};
  const ses = rm.session;
  const urls = lan.urls || [];
  return `
  <div class="card note warn" style="border:2px solid #ff7a59;background:rgba(255,122,89,.12);color:#ffb499;text-align:center;padding:16px;font-size:18px;font-weight:800;letter-spacing:1px;margin-bottom:14px">
    ⚠ 该功能还未开发成功，请等待 V1.0.1 版本发布
  </div>
  <div class="sec"><div class="sec-h">局域网访问</div>
    <div class="field"><label>允许手机访问<span class="hint">关闭后只有本机可连</span></label>${sw(lan.enabled, 'lan_enabled')}</div>
    ${lan.enabled ? `
      <div class="note">手机与电脑连同一个 Wi-Fi（或同一 ZeroTier / Tailscale 虚拟内网）。用手机相机扫下面二维码即可直连，不用手输 IP：</div>
      <div class="qr-box" id="qrBox">
        <canvas id="qrCanvas" width="176" height="176"></canvas>
        <div class="qr-side">
          <div class="qr-url" id="qrUrl"></div>
          <button class="btn sm" id="btnCopyUrl" style="margin-top:8px">复制地址</button>
          <div class="note" style="margin-top:8px;font-size:11px">换 Wi-Fi 或 IP 变化后，重新扫一次这个码即可。</div>
        </div>
      </div>
      ${urls.map((u) => `<div class="url-box"><span>${esc(u)}</span>
        <button class="btn sm" data-copytext="${esc(u)}" style="margin-left:auto">复制</button></div>`).join('') || '<div class="note">未检测到局域网 IP</div>'}
    ` : '<div class="note">开启后可在手机端查看会话、授权远控、并在 Lumen 未启动时唤起它。</div>'}
  </div>

  <div class="sec"><div class="sec-h">跨网中继（电脑在别处也能连）</div>
    <div class="note">不想只在同一 Wi-Fi 下用？自己搭一个中继（<b>tools/relay_server.py</b>，一条命令起，纯标准库），
      电脑侧的 LumenGuard 会主动拨号连上去，手机用流量也能连、也能唤醒主 AI。</div>
    <div class="field"><label>启用中继<span class="hint">关闭则只在局域网内可连</span></label>${sw((S.settings.relay || {}).enabled === true, 'relay.enabled')}</div>
    <div class="field"><label>中继地址<span class="hint">形如 http://你的中继:8772</span></label>
      <input type="text" id="f-relay-host" value="${esc((S.settings.relay || {}).host || '')}" placeholder="http://relay.example.com:8772" style="width:230px"></div>
    <div class="field"><label>中继密钥<span class="hint">与中继启动时 --secret 一致</span></label>
      <input type="password" id="f-relay-secret" value="${esc((S.settings.relay || {}).secret || '')}" placeholder="与中继端一致" style="width:200px"></div>
    <div class="btn-row"><button class="btn sm primary" id="btnSaveRelay">保存并重连</button>
      <button class="btn sm" id="btnRelayStatus">查看连接</button></div>
    <div id="relayState" class="note"></div>
    <div class="doc" style="margin-top:8px">手机端连接：用浏览器打开 <b>中继地址</b>，输入同一把密钥，即可跳转到 Lumen 手机端（数据只过你的中继，不落地第三方）。</div>
  </div>

  <div class="sec"><div class="sec-h">配对</div>
    <div class="field"><label>一次性配对码<span class="hint">5 分钟内有效，手机端输入即可登录</span></label>
      <button class="btn sm primary" id="btnPair">生成</button></div>
    <div class="url-box" id="pairBox" hidden></div>
  </div>

  <div class="sec"><div class="sec-h">Windows 防火墙</div>
    <div class="card" id="fwCard"><div class="note">正在检查防火墙状态…</div></div>
    <div class="btn-row">
      <button class="btn sm primary" id="btnFwAllow">一键放行局域网</button>
      <button class="btn sm" id="btnFwRefresh">重新检查</button>
    </div>
    <div class="note">Windows 的入站规则按<b>程序路径</b>匹配，绿色版换个目录就会重新触发
      「Windows Defender 防火墙已阻止此应用的部分功能」。更坑的是在弹窗上点<b>取消</b>会留下一条
      <b>拦截</b>规则，而拦截优先于放行——这就是「明明点了允许也没用」的原因。<br>
      Lumen 这里按<b>端口</b>补规则（8770 只允许本地子网访问），搬家也有效；同时会清掉历史遗留的拦截规则。</div>
  </div>

  <div class="sec"><div class="sec-h">已登录设备</div>
    ${(S.devices || []).length ? S.devices.map((d) => `
      <div class="card">
        <div class="kv"><span>${esc(d.name)} ${d.online ? '<span class="pill good">在线</span>' : ''}</span>
          <button class="btn sm danger" data-revoke="${esc(d.token_hint)}">吊销</button></div>
        <div class="note">最近活动 ${d.last_seen ? new Date(d.last_seen * 1000).toLocaleString('zh-CN') : '—'}</div>
      </div>`).join('') : '<div class="note">还没有手机登录过。</div>'}
  </div>

  <div class="sec"><div class="sec-h">远程控制</div>
    ${ses ? `
      <div class="card" style="border-color:rgba(95,211,182,.35)">
        <div class="kv"><span><span class="pill good">远控中</span> ${esc(ses.device)}</span>
          <button class="btn sm danger" id="btnStopRemote">断开</button></div>
        <div class="note">开始于 ${new Date(ses.started * 1000).toLocaleTimeString('zh-CN')} ·
          移动 ${((rm.metrics || {}).moves) || 0} 次 · 刷新率 ${((rm.metrics || {}).hz) || 0}Hz</div>
      </div>` : `
      <div class="note">手机请求远控时，这台电脑会弹出授权框，点「允许」才会开始。
        光标按「目标坐标 + 速度」插值移动，不是点哪瞬移哪。</div>`}
    <div class="field"><label>允许远控<span class="hint">关闭后一律拒绝</span></label>
      ${sw((S.settings.remote || {}).allow_control !== false, 'allow_control')}</div>
    <div class="field"><label>空闲自动断开<span class="hint">手机无操作多久后自动结束远控</span></label>
      <select id="f-idle">
        ${[[0, '不自动断开'], [300, '5 分钟'], [900, '15 分钟'], [1800, '30 分钟'], [3600, '1 小时']]
          .map(([v, t]) => `<option value="${v}" ${Number((S.settings.remote || {}).idle_timeout ?? 900) === v ? 'selected' : ''}>${t}</option>`).join('')}
      </select></div>
  </div>`;
}

function drawerGuard() {
  return `
  <div class="sec"><div class="sec-h">LumenGuard</div>
    <div class="card" id="guardCard"><div class="note">正在查询守护服务状态…</div></div>
    <div class="btn-row">
      <button class="btn sm" id="btnGuardStart">启动 Lumen</button>
      <button class="btn sm" id="btnGuardStop">关闭 Lumen</button>
      <button class="btn sm" id="btnGuardStatus">刷新状态</button>
    </div>
  </div>
  <div class="sec"><div class="sec-h">它能做什么</div>
    <div class="doc"><ul>
      <li>开机自启，随系统进入后台，常驻内存 &lt; 30MB</li>
      <li>手机端在 Lumen 未启动时，可通过账号通道请求它把 Lumen 拉起来</li>
      <li>检测到 Lumen 主进程退出后按恢复策略自动拉起</li>
    </ul></div>
  </div>
  <div class="sec"><div class="sec-h">它不能做什么（运维红线）</div>
    <div class="doc"><ul>
      <li>不做隐蔽进程、不做反任务管理器、不做 Rootkit</li>
      <li>不实现「任务管理器结束不了」——只能在自己被停掉之前把 Lumen 拉起来</li>
      <li>管理员可随时在 services.msc 停止 / 禁用 / 卸载</li>
    </ul></div>
    <div class="btn-row"><button class="btn sm danger" id="btnGuardUninstall">卸载 LumenGuard</button></div>
  </div>`;
}

function drawerAbout() {
  const t = tierById(S.model);
  const src = t.show_source && t.source
    ? `<div class="kv"><span>底层来源</span><b>${esc(t.source)}</b></div>`
    : `<div class="note">本档位不在主界面展示底层来源；第三方许可证要求的版权与许可声明完整保留在
       <b>licenses/${esc((t.notice || '').split('/').pop())}</b>，该文件不可删除。</div>`;
  return `
  <div class="sec"><div class="sec-h">关于 Lumen</div>
    <div class="card">
      <div class="kv"><span>版本</span><b>${esc(BOOT.version || '1.0.0')}</b></div>
      <div class="kv"><span>当前档位</span><b>${esc(t.name || '')}</b></div>
      <div class="kv"><span>参数量</span><b>${esc(t.params || '')}</b></div>
      <div class="kv"><span>量化</span><b>${esc(t.quant || '')}</b></div>
      <div class="kv"><span>运行时</span><b>${esc(t.runtime_label || '')}</b></div>
      ${src}
      <div class="kv"><span>标注</span><b>${esc(t.credit || '')}</b></div>
    </div>
    <div class="note">所有推理、文件操作、代码修改都在本机执行。手机同步走局域网 / 虚拟内网，不经过第三方服务器。</div>
  </div>

  <div class="sec"><div class="sec-h">档位一览</div>
    ${S.tiers.map((x) => `<div class="card">
      <div class="kv"><span>${esc(x.name)} <span class="pill ${x.available ? 'good' : 'bad'}">${x.available ? '可用' : '需 ' + x.min_vram_gb + 'GB'}</span></span><b>${esc(x.badge || '')}</b></div>
      <div class="note">${esc((x.strengths || []).slice(0, 2).join(' · '))}</div>
    </div>`).join('')}
  </div>

  <div class="sec"><div class="sec-h">这些模型是谁写的</div>
    <div class="doc"><ul>
      <li><b>Lumen H1 Turbo</b>：由<b>想念也是麻烦基于GPT模型大幅度修改并提升能力及智商</b>。</li>
      <li><b>Lumen V1 Ultra / mini se / Core 3B</b>：由<b>「想念也是麻烦」修改并增强</b>。</li>
      <li>各档位的上游来源与许可证声明见 <b>licenses/</b> 目录（安装包内必须保留，不可删除）。</li>
      <li>「档位」是能力定位，「权重」才是真正跑起来的东西——界面显示的档位名与实际加载的权重永远一致。</li>
    </ul></div>
  </div>

  <div class="sec"><div class="sec-h">合规声明</div>
    <div class="doc"><ul>
      <li>许可文件目录：<b>licenses/</b>（安装包内必须保留，不可删除）</li>
      <li>V1 Ultra 需 PrismML 定制版 llama.cpp，请勿用 Ollama 直接加载 PTQ1_0</li>
      <li>不实现无授权远控；远控必须在本机弹窗确认</li>
      <li>LumenGuard 随 Lumen 必装，可在 services.msc 停止与卸载</li>
    </ul></div>
  </div>`;
}

function bindDrawer() {
  // 开关
  $$('[data-sw]').forEach((el) => el.addEventListener('click', async () => {
    const key = el.dataset.sw;
    const on = !el.classList.contains('on');
    const patch = {};
    if (key === 'force_tier') {
      patch.force_tier = on;
    } else if (key === 'lan_enabled') {
      patch.remote = { lan_enabled: on };
    } else if (key === 'allow_control') {
      patch.remote = { allow_control: on };
    } else {
      const [a, b] = key.split('.');
      patch[a] = { [b]: on };
    }
    try {
      const r = await api('/api/settings', { settings: patch });
      S.settings = r.settings;
      el.classList.toggle('on', on);
      if (key === 'lan_enabled') {
        const l = await api('/api/lan');
        S.lan = l;
        renderDrawer();
        toast(on ? '局域网访问已开启' : '已关闭局域网访问', 'ok');
      }
      if (key === 'agent.enabled' || key === 'force_tier') renderTopbar();
    } catch (e) { toast(e.message, 'err'); }
  }));

  const on = (id, ev, fn) => { const el = $('#' + id); if (el) el.addEventListener(ev, fn); };

  on('btnSaveEngine', 'click', async () => {
    try {
      const r = await api('/api/settings', {
        settings: {
          engine: {
            base_url: $('#f-base').value.trim(),
            model: realModelName($('#f-model').value.trim()),
            num_ctx: +$('#f-ctx').value,
            temperature: +$('#f-temp').value,
          },
        },
      });
      S.settings = r.settings;
      S.engine = await api('/api/engine/rediscover');
      renderTopbar();
      toast(S.engine.online ? '引擎已连接' : '未探测到端点，仍使用离线引擎', S.engine.online ? 'ok' : 'err');
      renderDrawer();
    } catch (e) { toast(e.message, 'err'); }
  });
  on('btnRedetect', 'click', async () => {
    S.engine = await api('/api/engine/rediscover');
    renderTopbar();
    toast(S.engine.online ? '已连接本地引擎' : '未发现本地模型端点', S.engine.online ? 'ok' : 'err');
    renderDrawer();
  });
  on('btnLcStart', 'click', async () => {
    toast('正在启动本地运行时…');
    try {
      const r = await api('/api/llamacpp/start', {});
      S.engine = r.engine;
      renderTopbar();
      renderDrawer();
      toast(r.ok ? '本地运行时已就绪' : (r.llamacpp && r.llamacpp.log ? r.llamacpp.log.slice(0, 120) : '启动失败'), r.ok ? 'ok' : 'err');
    } catch (e) { toast(e.message, 'err'); }
  });
  on('btnLcStop', 'click', async () => {
    try {
      const r = await api('/api/llamacpp/stop', {});
      S.engine = r.engine;
      renderTopbar();
      renderDrawer();
      toast(r.stopped ? '本地运行时已停止' : '没有由 Lumen 启动的运行时', 'ok');
    } catch (e) { toast(e.message, 'err'); }
  });
  on('btnSaveSearch', 'click', async () => {
    const r = await api('/api/settings', {
      settings: { search: { mode: $('#f-mode').value, threshold: +$('#f-thr').value, searxng_url: $('#f-searx').value.trim() } },
    });
    S.settings = r.settings;
    toast('搜索设置已保存', 'ok');
  });
  on('btnTestSearch', 'click', async () => {
    const q = prompt('检索关键词', '本地大模型 8GB 显存');
    if (!q) return;
    toast('检索中…');
    try {
      const r = await api('/api/search?q=' + encodeURIComponent(q));
      toast(`${r.provider || '无'} · ${r.results.length} 条 · ${r.took_ms}ms`, r.results.length ? 'ok' : 'err');
    } catch (e) { toast(e.message, 'err'); }
  });
  on('btnOpenWs', 'click', () => { window.open('file:///' + (BOOT.ws || ''), '_blank'); });
  on('btnFwRefresh', 'click', async () => { await loadFirewall(); });
  on('btnFwAllow', 'click', async () => {
    toast('正在写入防火墙规则…');
    try {
      const r = await api('/api/firewall/allow', {});
      toast(r.message || '已处理', r.ok ? 'ok' : 'err');
      await loadFirewall();
    } catch (e) { toast(e.message, 'err'); }
  });
  on('btnSavePwd', 'click', async () => {
    const p = $('#f-pwd').value;
    try {
      const r = await api('/api/account/password', { password: p });
      S.account = r.account;
      toast('密码已更新', 'ok');
      renderDrawer();
    } catch (e) { toast(e.message, 'err'); }
  });
  on('btnPair', 'click', async () => {
    const r = await api('/api/paircode', {});
    const box = $('#pairBox');
    box.hidden = false;
    box.innerHTML = `<span>配对码 <b style="font-size:17px;letter-spacing:.18em">${esc(r.code)}</b></span>`;
    toast('5 分钟内有效', 'ok');
  });
  on('btnSaveRelay', 'click', async () => {
    const host = ($('#f-relay-host') || {}).value || '';
    const secret = ($('#f-relay-secret') || {}).value || '';
    const enabled = !!($('[data-sw="relay.enabled"]') || {}).classList.contains('on');
    try {
      const r = await api('/api/settings', { settings: { relay: { enabled, host, secret } } });
      S.settings = r.settings;
      await api('/api/relay/reload', {});
      toast('已保存并通知守护进程重连', 'ok');
      showRelayState();
    } catch (e) { toast(e.message, 'err'); }
  });
  on('btnRelayStatus', 'click', () => showRelayState());
  $$('[data-revoke]').forEach((b) => b.addEventListener('click', async () => {
    toast('请在手机端退出，或在下方吊销后重新配对');
  }));
  $$('[data-copytext]').forEach((b) => b.addEventListener('click', () => {
    navigator.clipboard.writeText(b.dataset.copytext);
    toast('已复制', 'ok');
  }));
  on('btnCopyUrl', 'click', () => {
    const u = ((S.lan && S.lan.urls) || [])[0] || '';
    if (!u) { toast('未检测到局域网 IP', 'err'); return; }
    navigator.clipboard.writeText(u);
    toast('已复制', 'ok');
  });
  const idleSel = $('#f-idle');
  if (idleSel) {
    idleSel.addEventListener('change', async () => {
      try {
        const r = await api('/api/settings', { remote: { idle_timeout: Number(idleSel.value) } });
        S.settings = r.settings;
        toast(Number(idleSel.value) ? `远控空闲 ${Math.round(Number(idleSel.value) / 60)} 分钟自动断开` : '已关闭自动断开', 'ok');
      } catch (e) { toast(e.message, 'err'); }
    });
  }
  on('btnStopRemote', 'click', async () => {
    await api('/api/remote/stop', { by: 'desktop' });
    S.remote = await api('/api/remote/status');
    renderDrawer();
    toast('已断开远控', 'ok');
  });
  on('btnGuardStart', 'click', async () => {
    const r = await api('/api/guard', { action: 'START_LUMEN' });
    toast(r.ok ? '已请求启动 Lumen' : (r.error || '失败'), r.ok ? 'ok' : 'err');
  });
  on('btnGuardStop', 'click', async () => {
    const r = await api('/api/guard', { action: 'STOP_LUMEN' });
    toast(r.ok ? '已请求关闭 Lumen' : (r.error || '失败'), r.ok ? 'ok' : 'err');
  });
  on('btnGuardStatus', 'click', () => refreshGuard());
  on('btnGuardUninstall', 'click', () => {
    openModal({
      title: '卸载 LumenGuard',
      body: `<div class="note">请在 Windows 管理员命令提示符里执行安装目录下的 <b>uninstall_guard.bat</b>，
        或在「服务」管理器中删除 <b>LumenGuard</b> 服务。Lumen 不会阻止你卸载它。</div>`,
      narrow: true,
    });
  });
}

async function refreshGuard() {
  const card = $('#guardCard');
  if (!card) return;
  try {
    const r = await api('/api/guard/status');
    if (r.online === false || r.ok === false) {
      card.innerHTML = `<div class="kv"><span>状态</span><b style="color:var(--red)">未运行</b></div>
        <div class="note">执行安装目录下的 <b>install_guard.bat</b>（需管理员）即可注册为 Windows 服务，开机自启。</div>`;
    } else {
      card.innerHTML = `<div class="kv"><span>状态</span><b style="color:var(--teal)">运行中</b></div>
        <div class="kv"><span>PID</span><b>${r.pid || '—'}</b></div>
        <div class="kv"><span>Lumen 进程</span><b>${r.lumen_running ? '运行中' : '已停止'}</b></div>
        <div class="kv"><span>自动拉起</span><b>${r.restart_on_exit ? '开' : '关'}</b></div>
        <div class="kv"><span>常驻内存</span><b>${r.rss_mb || '—'} MB</b></div>
        <div class="kv"><span>累计拉起</span><b>${r.restarts || 0} 次</b></div>`;
    }
  } catch (e) {
    card.innerHTML = `<div class="note" style="color:var(--red)">LumenGuard 未运行（${esc(e.message)}）</div>`;
  }
}

/* ----------------------------------------------------------------- 弹窗 */
function openModal({ title, sub = '', body, footer = '', wide = false, narrow = false, onMount }) {
  const root = $('#modalRoot');
  root.classList.add('on');
  root.innerHTML = `<div class="modal ${wide ? '' : narrow ? 'narrow' : ''}">
    <div class="modal-h"><div><h3>${esc(title)}</h3>${sub ? `<div class="sub">${esc(sub)}</div>` : ''}</div>
      <button class="icon-btn" data-close><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>
    <div class="modal-b">${body}</div>
    ${footer ? `<div class="modal-f">${footer}</div>` : ''}
  </div>`;
  root.onclick = (e) => { if (e.target === root) closeModal(); };
  $$('[data-close]', root).forEach((b) => b.onclick = closeModal);
  if (onMount) onMount(root);
  return root;
}
function closeModal() {
  const root = $('#modalRoot');
  root.classList.remove('on');
  root.innerHTML = '';
  S.modal = null;
}

/* ---------------------------------------------------------- 审批 / 授权轮询 */
async function poll() {
  if (document.hidden) return;
  try {
    if (!S.modal) {
      const r = await api('/api/approvals');
      const t = (r.tools || [])[0];
      if (t && !S.approvalsShown.has(t.id)) { S.approvalsShown.add(t.id); showToolApproval(t); return; }
      const p = (r.remote || {}).pending;
      if (p && !S.approvalsShown.has('R' + p.id)) { S.approvalsShown.add('R' + p.id); showRemoteApproval(p); return; }
    }
    const st = await api('/api/remote/status');
    const wasActive = !!(S.remote && S.remote.session);
    S.remote = st;
    if (wasActive !== !!st.session) {
      if (S.drawer === 'phone') renderDrawer();
      toast(st.session ? '手机已开始远程控制' : '远控已结束', st.session ? 'ok' : '');
    }
    const dot = $('#phoneDot');
    dot.classList.toggle('live', !!st.session);
  } catch (e) { /* 服务可能在重启 */ }
}

function showToolApproval(t) {
  const kindMap = { shell: '执行命令', write: '写入文件', net: '访问网络' };
  openModal({
    title: 'AI 请求执行操作',
    sub: kindMap[t.kind] || t.kind,
    narrow: true,
    body: `<div class="approve-ico"><svg viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L2.5 17a2 2 0 001.7 3h15.6a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg></div>
      <div class="approve-meta">${esc(t.title || '')}</div>
      <div class="approve-code">${esc(t.detail || '')}</div>
      <div class="note">确认后 Lumen 才继续。这个操作会在本机真实执行。</div>`,
    footer: `<button class="btn" data-no>拒绝</button><button class="btn primary" data-yes>允许执行</button>`,
    onMount: (root) => {
      $('[data-yes]', root).onclick = async () => {
        await api('/api/approve', { id: t.id, approved: true });
        closeModal(); toast('已允许', 'ok');
      };
      $('[data-no]', root).onclick = async () => {
        await api('/api/approve', { id: t.id, approved: false, note: '用户拒绝' });
        closeModal(); toast('已拒绝');
      };
    },
  });
}

function showRemoteApproval(p) {
  openModal({
    title: '远程控制请求',
    sub: esc(p.device),
    narrow: true,
    body: `<div class="approve-ico"><svg viewBox="0 0 24 24"><rect x="7" y="2" width="10" height="20" rx="2.5"/><path d="M11 18.5h2"/></svg></div>
      <div class="approve-meta">设备 <b>${esc(p.device)}</b> 请求控制这台电脑。</div>
      <div class="note" style="margin-top:8px">允许后对方可以移动光标、点击、输入。你可以随时在「手机 → 远程控制」里一键断开，
        或直接把鼠标拽一下（远控会话会保持，但你能看到光标的每一次移动）。</div>`,
    footer: `<button class="btn" data-no>拒绝</button><button class="btn primary" data-yes>允许远控</button>`,
    onMount: (root) => {
      $('[data-yes]', root).onclick = async () => {
        await api('/api/remote/decide', { approved: true });
        closeModal(); toast('已授权远控', 'ok');
        openDrawer('phone');
      };
      $('[data-no]', root).onclick = async () => {
        await api('/api/remote/decide', { approved: false });
        closeModal(); toast('已拒绝');
      };
    },
  });
}

/* ---------------------------------------------------------------- 事件 */
function autoGrow() {
  const t = $('#input');
  t.style.height = 'auto';
  t.style.height = Math.min(220, t.scrollHeight) + 'px';
}

function bind() {
  $('#btnSend').addEventListener('click', () => {
    if (S.streaming && S.ctrl) { S.ctrl.abort(); return; }
    send($('#input').value);
  });
  $('#input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send($('#input').value); }
  });
  $('#input').addEventListener('input', autoGrow);

  $('#btnNew').addEventListener('click', newChat);
  $('#btnCollapse').addEventListener('click', () => $('#app').classList.toggle('collapsed'));
  $('#modelPill').addEventListener('click', openModelPicker);
  $('#btnPanel').addEventListener('click', () => openDrawer('settings'));
  $('#btnPhone').addEventListener('click', () => openDrawer('phone'));
  $('#btnAbout').addEventListener('click', () => openDrawer('about'));
  $('#drawerClose').addEventListener('click', closeDrawer);
  $$('.dtab').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));

  $('#btnSearchToggle').addEventListener('click', async () => {
    const on = !((S.settings.search || {}).enabled);
    const r = await api('/api/settings', { settings: { search: { enabled: on } } });
    S.settings = r.settings;
    $('#btnSearchToggle').classList.toggle('on', on);
    $('#chipSearch').dataset.on = on ? '1' : '0';
    toast(on ? '联网搜索已开启' : '联网搜索已关闭', 'ok');
  });

  $('#chipAgent').addEventListener('click', async () => {
    const on = !((S.settings.agent || {}).enabled);
    const r = await api('/api/settings', { settings: { agent: { enabled: on } } });
    S.settings = r.settings;
    $('#chipAgent').dataset.on = on ? '1' : '0';
    renderTopbar();
  });
  $('#chipSearch').addEventListener('click', () => $('#btnSearchToggle').click());
  $('#chipWork').addEventListener('click', () => {
    api('/api/settings', {}).then(() => toast('工作区：Lumen\\workspace（可在设置里打开）'));
  });

  $('#btnExplore').addEventListener('click', async () => {
    const q = prompt('检索并查看原始结果', '');
    if (!q) return;
    const r = await api('/api/search?q=' + encodeURIComponent(q));
    openModal({
      title: '检索结果',
      sub: `${r.provider || '—'} · ${r.results.length} 条 · ${r.took_ms}ms${r.cached ? ' · 缓存' : ''}`,
      wide: true,
      body: (r.results || []).map((x, i) => `<div class="src" style="padding:9px 4px">
        <span class="src-i">${i + 1}</span>
        <span class="src-t"><b>${esc(x.title)}</b><span>${esc(x.snippet || x.url)}</span>
        <a href="${esc(x.url)}" target="_blank" rel="noreferrer" style="font-size:11px">${esc(x.url)}</a></span></div>`).join('') || '<div class="note">没有结果</div>',
    });
  });

  $('#btnMore').addEventListener('click', () => openDrawer('settings'));

  $('#stage').addEventListener('click', (e) => {
    const card = e.target.closest('.pcard');
    if (card) { $('#input').value = card.dataset.p; autoGrow(); $('#input').focus(); }
    const cp = e.target.closest('[data-copy]');
    if (cp) {
      const code = cp.closest('.code-wrap').querySelector('code').innerText;
      navigator.clipboard.writeText(code);
      cp.textContent = '已复制';
      setTimeout(() => (cp.textContent = '复制'), 1400);
    }
    const cm = e.target.closest('[data-copy-msg]');
    if (cm) {
      const m = S.messages[+cm.dataset.copyMsg];
      if (m) { navigator.clipboard.writeText(m.content); toast('已复制', 'ok'); }
    }
    const rg = e.target.closest('[data-regen]');
    if (rg && !S.streaming) {
      const i = +rg.dataset.regen;
      for (let k = i - 1; k >= 0; k--) {
        if (S.messages[k].role === 'user') { send(S.messages[k].content); break; }
      }
    }
    const tc = e.target.closest('.tool-card');
    if (tc) tc.classList.toggle('expanded');
  });

  $('#sessionList').addEventListener('click', async (e) => {
    const del = e.target.closest('[data-del]');
    if (del) {
      e.stopPropagation();
      await api('/api/session/delete', { id: del.dataset.del });
      if (S.cur === del.dataset.del) newChat();
      loadSessions();
      return;
    }
    const row = e.target.closest('[data-sid]');
    if (row) openSession(row.dataset.sid);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeModal(); if (S.drawer) closeDrawer(); }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'n') { e.preventDefault(); newChat(); }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); $('#input').focus(); }
  });
}

/* ---------------------------------------------------------------- 启动 */
async function init() {
  try {
    const b = await api('/api/bootstrap');
    Object.assign(S, {
      sessions: b.sessions || [], tiers: b.tiers || [], hw: b.hardware || {},
      engine: b.engine || {}, settings: b.settings || {}, account: b.account || {},
      devices: b.devices || [], lan: b.lan || {}, remote: b.remote || {},
      model: b.current_tier || 'h1', seenSeq: b.seq || 0,
    });
    BOOT.ws = b.license_dir ? '' : '';
    renderTopbar();
    renderSidebar();
    renderWelcome();
    renderThread();
    $('#btnSearchToggle').classList.toggle('on', !!(S.settings.search || {}).enabled);
    $('#chipAgent').dataset.on = (S.settings.agent || {}).enabled ? '1' : '0';
    $('#chipSearch').dataset.on = (S.settings.search || {}).enabled ? '1' : '0';
    if (!b.local) {
      $('#composerTip').textContent = `已以「${b.device || '移动设备'}」身份连接 · 远控需电脑端授权`;
    }
    if (b.sessions && b.sessions.length) openSession(b.sessions[0].id);
  } catch (e) {
    toast('初始化失败：' + e.message, 'err');
  } finally {
    $('#boot').style.display = 'none';
    $('#app').hidden = false;
    $('#input').focus();
  }
  poll();
  setInterval(poll, 1500);
}

document.addEventListener('DOMContentLoaded', () => { bind(); init(); });
})();
