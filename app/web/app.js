/* ═══════════════════════════════════════════════════════════
   声文 SoundScribe · 前端逻辑
   ═══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  function fmtDur(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`
             : `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
  }
  function fmtSize(n) {
    let v = n || 0; const u = ['B','KB','MB','GB'];
    let i = 0; while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return v.toFixed(v < 10 ? 1 : 0) + ' ' + u[i];
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
  function toast(msg, kind) {
    const el = document.createElement('div');
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.textContent = msg;
    $('#toasts').appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, kind === 'bad' ? 7000 : 4200);
  }
  const basename = p => String(p || '').split(/[\\/]/).pop();

  const STAGES = [
    ['probe', '探测媒体'], ['extract', '抽取音轨'], ['route', '语言路由'],
    ['load', '加载引擎'], ['transcribe', '语音识别'], ['postprocess', '后处理与校验'], ['export', '导出文件'],
  ];
  const VIEW_TITLE = { workbench: '工作台', library: '文稿库', models: '模型中心', settings: '导出与词库', diag: '诊断报告' };

  const state = {
    env: null, jobs: [], current: null, es: null, segs: [],
    staging: [],          // 待开始队列
    picked: new Set(),    // 被勾选的任务 id（任务列表与文稿库共用）
    downloads: {},        // model_id -> download 状态
    storage: null,        // 磁盘占用 + 可清理项清单
    caps: null,           // ★ 能力矩阵（/api/capabilities）—— "生效范围"文案的唯一来源
    corrections: null,    // 纠错规则表（含标定来源核对）
    dlTimer: null, jobTimer: null,
    settings: {
      engine: 'auto', timeline: 'segment', vad: 1, normalize: 0,
      layout: 'by_date', formats: ['txt', 'srt', 'vtt', 'md'],
      template: '{stem}_{engine}_{date}',
    },
  };
  let stagedSeq = 0;

  /* ═════════════ 粒子背景 ═════════════ */
  const canvas = $('#stars'), ctx = canvas.getContext('2d');
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const budget = (navigator.hardwareConcurrency || 8) <= 4 ? 0.5 : 1;
  let W = 0, H = 0, dpr = Math.min(window.devicePixelRatio || 1, 2);
  let parts = [], pointer = { x: .5, y: .5, tx: .5, ty: .5 }, rafId = null, active = true;

  const HUES_SPACE = [[255,255,255],[168,240,228],[185,176,255],[255,255,255],[255,255,255]];
  const HUES_DAY = [[60,90,130],[15,110,86],[90,80,170],[70,95,130]];
  const hues = () => document.documentElement.getAttribute('data-theme') === 'space' ? HUES_SPACE : HUES_DAY;

  function buildStars() {
    W = canvas.width = Math.floor(innerWidth * dpr);
    H = canvas.height = Math.floor(innerHeight * dpr);
    canvas.style.width = innerWidth + 'px'; canvas.style.height = innerHeight + 'px';
    const n = Math.round(Math.min(180, Math.round(innerWidth * innerHeight / 13500)) * budget);
    const hl = hues();
    parts = [];
    for (let i = 0; i < n; i++) parts.push({
      x: Math.random()*W, y: Math.random()*H,
      r: (Math.random()*1.2 + .35)*dpr,
      vx: (Math.random()-.5)*.06*dpr, vy: (Math.random()-.5)*.06*dpr,
      base: Math.random()*.45 + .2, tw: Math.random()*Math.PI*2, tws: Math.random()*.015 + .004,
      depth: Math.random()*.85 + .15, c: hl[(Math.random()*hl.length)|0],
    });
  }
  function drawStars() {
    if (!active) { rafId = null; return; }
    ctx.clearRect(0, 0, W, H);
    pointer.x += (pointer.tx - pointer.x)*.045;
    pointer.y += (pointer.ty - pointer.y)*.045;
    const ox = (pointer.x-.5)*24*dpr, oy = (pointer.y-.5)*24*dpr;
    for (const p of parts) {
      if (!reduce) {
        p.x += p.vx; p.y += p.vy; p.tw += p.tws;
        if (p.x < -10) p.x = W+10; if (p.x > W+10) p.x = -10;
        if (p.y < -10) p.y = H+10; if (p.y > H+10) p.y = -10;
      }
      let a = p.base + Math.sin(p.tw)*.22; if (a < .04) a = .04;
      const x = p.x + ox*p.depth, y = p.y + oy*p.depth;
      ctx.beginPath(); ctx.arc(x, y, p.r*(.7 + p.depth*.6), 0, Math.PI*2);
      ctx.fillStyle = `rgba(${p.c[0]},${p.c[1]},${p.c[2]},${a.toFixed(3)})`; ctx.fill();
      if (p.depth > .72 && a > .42) {
        ctx.beginPath(); ctx.arc(x, y, p.r*3.6, 0, Math.PI*2);
        ctx.fillStyle = `rgba(${p.c[0]},${p.c[1]},${p.c[2]},0.05)`; ctx.fill();
      }
    }
    rafId = requestAnimationFrame(drawStars);
  }
  addEventListener('resize', buildStars);
  addEventListener('mousemove', e => { pointer.tx = e.clientX/innerWidth; pointer.ty = e.clientY/innerHeight; }, { passive: true });
  document.addEventListener('visibilitychange', () => {
    active = !document.hidden;
    if (active && rafId === null) rafId = requestAnimationFrame(drawStars);
  });
  buildStars(); rafId = requestAnimationFrame(drawStars);

  /* ═════════════ 主题 ═════════════ */
  function applyTheme(v) {
    try { localStorage.setItem('sb.theme', v); } catch (_) {}
    let r = v;
    if (v === 'auto') r = matchMedia('(prefers-color-scheme: dark)').matches ? 'space' : 'day';
    document.documentElement.setAttribute('data-theme', r);
    $$('#themeswitch button').forEach(b => b.classList.toggle('on', b.dataset.set === v));
    buildStars();
  }
  $$('#themeswitch button').forEach(b => b.addEventListener('click', () => applyTheme(b.dataset.set)));
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    const on = $('#themeswitch button.on');
    if (on && on.dataset.set === 'auto') applyTheme('auto');
  });

  /* ═════════════ 视图切换 ═════════════ */
  const VIEWS = ['workbench', 'library', 'models', 'settings', 'diag'];
  function go(view, silent) {
    if (!VIEWS.includes(view)) view = 'workbench';
    $$('#nav button').forEach(x => x.classList.toggle('on', x.dataset.view === view));
    $$('.view').forEach(v => v.classList.toggle('on', v.id === 'view-' + view));
    if (!silent) { try { history.replaceState(null, '', '#' + view); } catch (_) {} }
    if (view === 'library') renderLibrary();
    if (view === 'settings') loadStorage();
    if (view === 'diag') { loadHealth(false); }
  }
  $$('#nav button').forEach(b => b.addEventListener('click', () => go(b.dataset.view)));
  addEventListener('hashchange', () => go((location.hash || '').replace('#', ''), true));

  /* ═════════════ API ═════════════ */
  async function api(path, opts) {
    const r = await fetch(path, opts);
    const ct = r.headers.get('content-type') || '';
    const data = ct.includes('json') ? await r.json() : await r.text();
    if (!r.ok) throw new Error((data && data.detail) ? data.detail : ('请求失败 ' + r.status));
    return data;
  }
  const postJSON = (p, body) => api(p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

  /* ═════════════ 环境 ═════════════ */
  async function loadEnv() {
    try {
      state.env = await api('/api/env');
      const e = state.env;
      const gpu = e.gpu && e.gpu.available ? e.gpu.name : '无独立显卡（走 CPU）';
      // 侧边栏窄：显卡名去掉厂商前缀，只留型号（"NVIDIA GeForce RTX 4070 Laptop GPU" → "RTX 4070"）
      const shortGpu = String(gpu)
        .replace(/^(NVIDIA|AMD|Intel)\s+/i, '')
        .replace(/^GeForce\s+/i, '')
        .replace(/^Radeon\s+/i, '')
        .replace(/\s+(Laptop|Desktop)\s+GPU$/i, '')
        .replace(/\s+GPU$/i, '');
      $('#envPill').textContent = `${e.tier} 档 · ${shortGpu}`;
      $('#envPill').title = gpu;
      $('#envPill').className = 'pill' + (e.tier === 'A' || e.tier === 'B' ? ' live' : '');
      // ★ 用「真正可用」的引擎（依赖+模型都齐），而不是只看库在不在。
      //   否则会出现"胶囊显示 SenseVoice 可用、但一跑就说模型没下载"的矛盾。
      const NICE = { whisper: 'Whisper', sensevoice: 'SenseVoice', parakeet: 'Parakeet', moonshine: 'Moonshine' };
      let en = (e.engines_ready || []).map(x => NICE[x.engine] || x.engine);
      if (!en.length) {                      // 老接口兜底
        if (e.engine.whisper_ok) en.push('Whisper');
        if (e.engine.sensevoice_ok) en.push('SenseVoice');
      }
      $('#enginePill').textContent = en.length ? '引擎：' + en.join(' + ') : '引擎不可用';
      $('#enginePill').className = 'pill' + (en.length ? '' : ' bad');

      // ★ 推理后端（CPU / GPU）也显示出来 —— 这是用户看得见才算数的事。
      //   以前界面上完全没有设备信息，只能靠猜"到底跑在什么上"。
      const sg = e.sherpa_gpu || null;
      if (sg) {
        const t = sg.available ? 'GPU（CUDA 加速）' : 'CPU';
        $('#devicePill').textContent = '推理：' + t;
        $('#devicePill').className = 'pill' + (sg.available ? '' : ' dim');
        $('#devicePill').title = sg.reason || '';
      }
      renderModels(e);
      syncEngineOptions();      // 引擎选项跟着"实际可用"走
      renderDiag();
      warnIfDegraded(e);
    } catch (err) {
      $('#envPill').textContent = '环境自检失败';
      $('#envPill').className = 'pill bad';
      toast('环境自检失败：' + err.message, 'bad');
    }
  }

  function warnIfDegraded(e) {
    const b = $('#wbBanner');
    const msgs = [];
    if (!e.engine.sensevoice_ok) msgs.push({
      k: 'warn',
      t: 'SenseVoice 引擎不可用（缺少 PyTorch）',
      d: '中文场景推荐使用 SenseVoice。安装方式：pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu',
    });
    if (!e.gpu.available) msgs.push({
      k: 'warn',
      t: '未检测到 NVIDIA 显卡，将使用 CPU 模式',
      d: 'Whisper 在 CPU 上速度较慢（1 小时录音约需 18 分钟以上）。建议中文素材使用 SenseVoice，速度约快 8 倍。',
    });
    if (e.disk_free_gb && e.disk_free_gb < 10) msgs.push({
      k: 'bad', t: '磁盘空间不足 10GB', d: '模型下载与中间音频可能失败，建议先清理磁盘。',
    });
    b.innerHTML = msgs.map(m => `
      <div class="banner ${m.k}"><div class="ico"></div><div class="body">
        <strong>${esc(m.t)}</strong><p>${esc(m.d)}</p></div></div>`).join('');
  }

  /* ═════════════ 模型中心 ═════════════ */
  function renderModels(env) {
    const e = env || state.env;
    if (!e) return;
    $('#tierHint').textContent = e.tier + ' 档 · ' + e.tier_reason;
    $('#hwMetrics').innerHTML = [
      ['硬件档位', e.tier + ' 档', 'accent'],
      ['显卡', e.gpu.available ? e.gpu.name : '无（CPU 模式）', ''],
      ['显存', e.gpu.available ? Math.round(e.gpu.vram_mib/1024) + ' GB' : '—', ''],
      ['显卡驱动', e.gpu.available ? e.gpu.driver : '—', ''],
      ['CPU', `${e.cpu_cores} 线程`, ''],
      ['内存', e.ram_gb + ' GB', ''],
      ['磁盘剩余', e.disk_free_gb + ' GB', e.disk_free_gb < 10 ? 'warn' : ''],
      ['ffmpeg', e.ffmpeg && e.ffmpeg.available ? e.ffmpeg.version : '未找到', e.ffmpeg && e.ffmpeg.available ? '' : 'warn'],
    ].map(([k, v, c]) => `<div class="metric"><p class="k">${k}</p><p class="v ${c}">${esc(v)}</p></div>`).join('');

    const models = e.models || [];
    const installed = models.filter(m => m.installed).length;
    const totalMb = models.filter(m => m.installed).reduce((a, m) => a + (m.installed_mb || 0), 0);
    $('#modelSummary').textContent = `已安装 ${installed} 个 · 占用 ${fmtSize(totalMb * 1024 * 1024)}`;

    // ── 为你推荐：结论先行 ──
    const rb = $('#recoBar');
    if (rb) {
      const picks = e.picks || {};
      const one = (label, p) => p
        ? `<span class="rpick"><span class="k">${label}</span>` +
          `<span class="v">${esc(p.name)}</span>` +
          `<span class="x">${p.installed ? '已安装' : '待下载'}</span></span>`
        : '';
      rb.innerHTML = `<p class="rt">${esc(e.reco_summary || '按本机配置推荐')}</p>` +
        `<div class="rpicks">${one('中文素材', picks['中文素材'] || picks[Object.keys(picks)[0]])}` +
        `${one('英文素材', picks['英文素材'] || picks[Object.keys(picks)[1]])}</div>`;
    }

    // 按分组插入分隔标题，让"中文 / 英文 / 备选"分块可读
    let lastGroup = '';
    $('#modelList').innerHTML = models.map(m => {
      const groupHead = m.group && m.group !== lastGroup
        ? `<div class="mgroup">${esc(m.group)}</div>` : '';
      lastGroup = m.group || lastGroup;
      return groupHead + modelCard(m);
    }).join('');

    function modelCard(m) {
      const stateMap = {
        ok: ['ok', '可用'], risky: ['warn', '勉强可用'],
        blocked: ['off', '已禁用'], planned: ['off', '后续版本'],
      };
      const [cls, label] = stateMap[m.state] || ['off', '—'];
      const dl = m.download || state.downloads[m.id];
      const running = dl && dl.status === 'running';

      // ── 右侧操作区 ──
      let act = '';
      if (m.planned) {
        act = `<button class="btn sm" disabled>未接入</button>`;
      } else if (running) {
        // ★ 下载中：两个按钮消失，只剩进度条 + 速度 + 停止
        const pct = (dl.pct || 0).toFixed(0);
        const spd = dl.speed_mbps ? `${dl.speed_mbps.toFixed(1)} MB/s` : '连接中…';
        const eta = dl.eta_seconds > 0
          ? ` · 约剩 ${dl.eta_seconds >= 60 ? Math.round(dl.eta_seconds / 60) + ' 分' : Math.round(dl.eta_seconds) + ' 秒'}`
          : '';
        const srcNote = dl.used_source
          ? `<span class="dlsrc">${dl.used_source === 'official' ? '海外官方源' : '镜像站'}</span>`
          : (dl.message && /镜像|官方/.test(dl.message) ? `<span class="dlsrc">${esc(dl.message)}</span>` : '');
        act = `<div class="dlwrap">
            <div class="dlbar"><i style="width:${dl.pct || 0}%"></i></div>
            <div class="dlmeta">
              <span class="dltxt">${pct}% · ${(dl.downloaded_mb || 0).toFixed(0)}/${(dl.total_mb || 0).toFixed(0)} MB · ${spd}${eta}</span>
              <button class="btn sm ghost" data-dl-cancel="${m.id}">停止</button>
            </div>
            ${dl.from_fallback ? '<span class="dlsrc warn">首选源失败，已自动切换</span>' : srcNote}
          </div>`;
      } else if (m.installed) {
        act = `<span class="pill live">已安装 ${fmtSize((m.installed_mb || 0) * 1024 * 1024)}</span>
               <button class="btn sm danger" data-del="${m.id}">删除</button>`;
      } else if (m.state === 'blocked') {
        act = `<button class="btn sm" disabled>不可选</button>`;
      } else {
        // ★ 两个源按钮：国内用户走镜像（默认），海外或镜像不通时走官方源。
        //   点任意一个都会立刻变成进度条，按钮消失。
        const sz = fmtSize((m.download_mb || m.size_mb || 0) * 1024 * 1024);
        act = `<div class="dlbtns">
            <button class="btn sm primary" data-dl="${m.id}" data-src="mirror">
              镜像站下载 <span class="sz">${sz}</span>
            </button>
            <span class="dlhint">默认 · 中国用户</span>
            <button class="btn sm ghost" data-dl="${m.id}" data-src="official">海外官方源下载</button>
          </div>`;
      }
      if (m.install_state === 'partial' && !running) {
        act = `<span class="pill bad">下载不完整</span>` + act;
      }

      const why = m.reasons && m.reasons.length
        ? `<div class="why"><strong style="font-weight:500">${m.state === 'blocked' ? '为什么不能选' : '注意'}：</strong>${esc(m.reasons.join('；'))}</div>`
        : '';

      // ── 量化档列表（三态）──
      const vars = (m.variants || []).map(v => {
        const mark = v.state === 'ok' ? '✓' : (v.state === 'risky' ? '!' : '✕');
        const tip = v.reasons && v.reasons.length ? ` title="${esc(v.reasons.join('；'))}"` : '';
        return `<span class="var ${v.state}"${tip}>${mark} ${esc(v.label)}${v.vram_mb ? ' · ' + esc(v.vram_display) : ' · 无需显卡'}</span>`;
      }).join('');

      const dlNote = dl && dl.status === 'failed'
        ? `<div class="why" style="border-color:var(--danger)">下载失败：${esc(dl.message)}</div>`
        : '';

      return `<div class="mrow ${m.state === 'blocked' ? 'blocked' : ''}">
        <div class="info">
          <p class="nm">${esc(m.name)}<span class="tag ${cls}">${label}</span>
            ${m.installed ? '<span class="tag ok">已安装</span>' : ''}
            ${m.recommended ? '<span class="tag reco">为你推荐</span>' : ''}
            ${m.recommended_label ? `<span class="tag off">推荐：${esc(m.recommended_label)}</span>` : ''}</p>
          <p class="ds">${esc(m.note)} · ${esc(m.languages)} · ${fmtSize((m.size_mb || 0) * 1024 * 1024)}${m.source ? ' · ' + esc(m.source) : ''}</p>
          ${vars ? `<div class="varlist">${vars}</div>` : ''}
          ${m.why && m.why.length && m.recommended
              ? `<p class="reco-why"><strong style="font-weight:500">为什么推荐它：</strong>${esc(m.why.join('；'))}</p>`
              : ''}
          ${why}${dlNote}
        </div>
        <div class="act">${act}</div>
      </div>`;
    }

    // 事件绑定
    $$('#modelList [data-dl]').forEach(b => b.addEventListener('click', () => downloadModel(b.dataset.dl, b, b.dataset.src || 'mirror')));
    $$('#modelList [data-del]').forEach(b => b.addEventListener('click', () => deleteModel(b.dataset.del)));
    $$('#modelList [data-dl-cancel]').forEach(b => b.addEventListener('click', () => cancelDownload(b.dataset.dlCancel)));
  }

  async function downloadModel(id, btn, source = 'mirror') {
    // 立即禁用两个按钮，避免连点两次触发两条下载
    const group = btn.closest('.dlbtns');
    if (group) $$('button', group).forEach(b => { b.disabled = true; });
    else btn.disabled = true;
    try {
      await postJSON(`/api/models/${encodeURIComponent(id)}/download`, { source });
      toast('已开始下载，可继续用其他功能');
      startDownloadPolling();
    } catch (e) {
      toast('无法下载：' + e.message, 'bad');
      if (group) $$('button', group).forEach(b => { b.disabled = false; });
      else btn.disabled = false;
    }
  }

  async function cancelDownload(id) {
    try { await postJSON(`/api/models/${encodeURIComponent(id)}/download/cancel`); toast('已请求停止下载'); }
    catch (e) { toast(e.message, 'bad'); }
  }

  async function deleteModel(id) {
    const m = (state.env && state.env.models || []).find(x => x.id === id);
    const nm = m ? m.name : id;
    const mb = m && m.installed_mb ? m.installed_mb : 0;
    if (!confirm(`确定删除「${nm}」吗？\n\n将删除本机上的模型文件（约 ${fmtSize(mb * 1024 * 1024)}）。\n内置引擎程序不受影响，之后可以重新下载。`)) return;
    try {
      const r = await api(`/api/models/${encodeURIComponent(id)}`, { method: 'DELETE' });
      toast(r.message || '已删除', 'ok');
      await loadEnv();
    } catch (e) { toast('删除失败：' + e.message, 'bad'); }
  }

  /* ── 下载进度轮询 ── */
  function startDownloadPolling() {
    if (state.dlTimer) return;
    state.dlTimer = setInterval(async () => {
      let r;
      try { r = await api('/api/models/downloads'); } catch (_) { return; }

      const prev = state.downloads;
      // ★ 状态跃迁才提示：不能每个轮询周期都弹一次
      (r.downloads || []).forEach(d => {
        const p = prev[d.model_id] || {};
        if (d.status !== p.status) {
          if (d.status === 'done') { toast('模型下载完成', 'ok'); loadEnv(); loadStorage(); }
          else if (d.status === 'failed') toast('模型下载失败：' + d.message, 'bad');
          else if (d.status === 'canceled') toast('下载已停止');
        }
      });

      state.downloads = {};
      (r.downloads || []).forEach(d => { state.downloads[d.model_id] = d; });

      // 安装状态与磁盘余量同步
      if (state.env) {
        state.env.disk_free_gb = r.disk_free_gb;
        (state.env.models || []).forEach(m => {
          const inst = (r.installs || {})[m.id];
          if (inst) {
            m.installed = inst.installed;
            m.install_state = inst.state;
            m.installed_mb = inst.installed_mb;
          }
        });
      }

      const running = (r.downloads || []).filter(d => d.status === 'running');
      const b = $('#mdlBadge');
      b.style.display = running.length ? '' : 'none';
      b.textContent = running.length;

      renderModels();

      if (!running.length) {
        clearInterval(state.dlTimer); state.dlTimer = null;
        await loadEnv();
      }
    }, 1200);
  }

  /* ═════════════ 拖拽 ═════════════ */
  const overlay = $('#dropoverlay');
  let dragDepth = 0;
  function hasFiles(e) {
    const dt = e.dataTransfer;
    if (!dt) return false;
    if (dt.types) for (const t of dt.types) if (t === 'Files') return true;
    return false;
  }
  addEventListener('dragenter', e => { if (!hasFiles(e)) return; e.preventDefault(); dragDepth++; overlay.classList.add('on'); });
  addEventListener('dragover', e => { if (!hasFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; });
  addEventListener('dragleave', e => { if (!hasFiles(e)) return; dragDepth = Math.max(0, dragDepth-1); if (!dragDepth) overlay.classList.remove('on'); });
  addEventListener('drop', e => {
    if (!hasFiles(e)) return;
    e.preventDefault(); dragDepth = 0; overlay.classList.remove('on');
    const files = Array.from(e.dataTransfer.files || []).filter(f =>
      /\.(mp4|mkv|mov|avi|flv|webm|wmv|m4v|ts|mpg|mpeg|3gp|mp3|wav|m4a|aac|flac|ogg|opus|wma|amr|aiff|ape)$/i.test(f.name));
    const skipped = (e.dataTransfer.files || []).length - files.length;
    if (!files.length) { toast('未识别到支持的音视频文件', 'bad'); return; }
    if (skipped > 0) toast(`已忽略 ${skipped} 个不支持的文件`);
    go('workbench');
    stageFiles(files);
  });
  $('#dropzone').addEventListener('click', () => $('#filepick').click());
  $('#filepick').addEventListener('change', e => {
    const f = Array.from(e.target.files || []);
    if (f.length) { go('workbench'); stageFiles(f); }
    e.target.value = '';
  });

  /* ═════════════ 待开始队列 ═════════════ */
  function stageFiles(files) {
    const entries = files.map(f => {
      const key = 'st' + (++stagedSeq);
      // ★ engine: '' 表示「跟随转写选项」。之前硬编码成 'auto'，
      //   结果它在发请求时无条件覆盖了转写选项里的选择 ——
      //   那个设置因此从来没生效过（实测：设 Parakeet 仍跑 SenseVoice）。
      return { key, file: f, name: f.name, size: f.size, path: '', media: {}, engine: '', uploading: true, error: '' };
    });
    state.staging.push(...entries);
    renderStaging();
    uploadStaged(entries);
  }

  async function uploadStaged(entries) {
    const fd = new FormData();
    entries.forEach(e => fd.append('files', e.file, e.name));
    try {
      const r = await api('/api/upload-batch', { method: 'POST', body: fd });
      const byName = {};
      (r.files || []).forEach(x => { byName[x.name] = x; });
      entries.forEach(e => {
        const got = byName[e.name];
        if (got && got.ok) {
          e.uploading = false;
          e.path = got.path;
          e.media = got.media || {};
          e.size = got.size || e.size;
        } else {
          e.uploading = false;
          e.error = (got && got.error) || '上传失败';
        }
      });
      if (r.fail_count) toast(`${r.fail_count} 个文件未能读取，已在列表中标注`, 'bad');
    } catch (err) {
      entries.forEach(e => { e.uploading = false; e.error = err.message; });
      toast('上传失败：' + err.message, 'bad');
    }
    renderStaging();
  }

  function renderStaging() {
    const list = state.staging;
    $('#stageCard').style.display = list.length ? '' : 'none';
    const ready = list.filter(s => s.path && !s.error);
    $('#stageHint').textContent = `共 ${list.length} 个文件 · 点「开始转写」后才会运行`;
    const b = $('#wfBadge');
    if (ready.length) { b.style.display = ''; b.textContent = ready.length; } else { b.style.display = 'none'; }

    if (!list.length) { $('#stageList').innerHTML = ''; return; }

    $('#stageList').innerHTML = list.map(s => {
      const m = s.media || {};
      const meta = s.error ? '这个文件无法转写'
        : s.uploading ? '正在读取…'
        : [m.size_display, m.resolution || '', m.audio_codec, m.channels === 1 ? '单声道' : (m.channels ? m.channels + ' 声道' : '')]
            .filter(Boolean).join(' · ');
      const tag = s.error ? `<span class="tag warn">${esc(s.error)}</span>`
        : s.uploading ? '<span class="tag off">读取中…</span>'
        : `<span class="tag ok">${esc(m.duration_display || '—')}</span>`;
      return `<div class="srow ${s.error ? 'bad' : ''}" data-key="${s.key}">
        <div class="sinfo">
          <p class="sname"><span class="fn">${esc(s.name)}</span>${tag}</p>
          <p class="smeta">${esc(meta)}</p>
        </div>
        <div class="sact">
          ${enginePickerHTML(s.engine || 'auto')}
          <button class="btn sm primary" data-act="start" ${s.uploading || s.error ? 'disabled' : ''}>开始</button>
          <button class="btn sm ghost" data-act="remove">移除</button>
        </div>
      </div>`;
    }).join('');

    $$('#stageList .srow').forEach(row => {
      const key = row.dataset.key;
      const item = state.staging.find(x => x.key === key);
      if (!item) return;
      const sel = $('[data-act="engine"]', row);
      if (sel) bindEnginePicker(sel, v => { item.engine = v; });
      const st = $('button[data-act="start"]', row);
      if (st) st.addEventListener('click', async () => {
        st.disabled = true;
        await startStagedOne(item);
      });
      const rm = $('button[data-act="remove"]', row);
      if (rm) rm.addEventListener('click', async () => {
        rm.disabled = true;
        if (item.jobId) {
          try { await api(`/api/jobs/${item.jobId}`, { method: 'DELETE' }); } catch (_) {}
        }
        state.staging = state.staging.filter(x => x.key !== key);
        renderStaging();
        if (item.jobId) refreshJobs();
      });
    });

    const totalDur = ready.reduce((a, s) => a + ((s.media || {}).duration || 0), 0);
    const totalSize = ready.reduce((a, s) => a + (s.size || 0), 0);
    $('#btnStartAll').disabled = ready.length === 0;
    $('#btnStartAll').textContent = ready.length ? `开始转写（${ready.length} 个）` : '开始转写';
    $('#stageNote').textContent = ready.length
      ? `合计时长 ${fmtDur(totalDur)} · ${fmtSize(totalSize)}`
      : '还没有可转写的文件';
  }

  $('#btnStageClear').addEventListener('click', async () => {
    if (!state.staging.length) return;
    const linked = state.staging.filter(s => s.jobId);
    if (linked.length && !confirm(`将移除 ${state.staging.length} 个待开始项（其中 ${linked.length} 个已在任务列表中记录，会一并删除记录）？`)) return;
    for (const s of linked) {
      try { await api(`/api/jobs/${s.jobId}`, { method: 'DELETE' }); } catch (_) {}
    }
    state.staging = [];
    renderStaging();
    refreshJobs();
  });

  /** 刷新页面后，把后端仍处于 pending 的任务还原回待开始列表。
   *  这样「拖进来先确认」这个状态不会因为刷新而丢失。 */
  function restoreStaging() {
    const known = new Set(state.staging.map(s => s.path));
    let added = 0;
    state.jobs.filter(j => j.status === 'pending').forEach(j => {
      if (known.has(j.source)) return;
      const m = j.media || {};
      state.staging.push({
        key: 'st_' + j.id, jobId: j.id, path: j.source,
        name: m.name || basename(j.source),
        size: m.size_bytes || 0, media: m,
        engine: '', uploading: false, error: '', restored: true,
      });
      added++;
    });
    if (added) {
      renderStaging();
      renderJobLists();   // ★ 必须重刷：否则同一批 pending 会在任务列表里重复出现
    }
  }

  $('#btnStartAll').addEventListener('click', async () => {
    const ready = state.staging.filter(s => s.path && !s.error);
    if (!ready.length) return;
    const btn = $('#btnStartAll');
    btn.disabled = true; btn.textContent = '正在创建任务…';
    const ids = [];
    const failures = [];
    for (const s of ready) {
      // 刷新后还原的条目已经有后端任务记录，直接复用，不重复创建
      if (s.jobId) { ids.push(s.jobId); continue; }
      try {
        // ★ 逐文件覆盖是允许的，但"没指定"必须解析成全局默认 ——
        //   直接传 s.engine（空串）会让后端 fallback 到 auto，
        //   那转写选项里的选择依然不生效。
        const r = await postJSON('/api/jobs',
          Object.assign({}, jobOptions(), { path: s.path, engine: s.engine || state.settings.engine }));
        (r.jobs || []).forEach(j => ids.push(j.id));
      } catch (e) {
        failures.push(`${s.name}：${e.message}`);
      }
    }
    if (ids.length) {
      try {
        await postJSON('/api/jobs/start', { ids });
        toast(`已开始 ${ids.length} 个任务，按顺序依次执行`, 'ok');
      } catch (e) { toast('启动失败：' + e.message, 'bad'); }
      state.staging = [];
      state.current = null;      // 同上：让界面切到刚启动的第一个任务
      state.segs = [];
      updateJobLive('', true);           // 新任务：清空实时行
      _jobCardScrolled = false;          // 允许这一次把任务卡片滚进视野
      setTimeout(scrollJobCardIntoView, 600);
      renderStaging();
      await refreshJobs();
      startJobPolling();
    } else {
      toast('没有任务被创建：' + (failures[0] || '未知原因'), 'bad');
      btn.disabled = false;
      renderStaging();
    }
    if (failures.length) toast(failures[0], 'bad');
  });

  /** 只开始待开始列表里的某一个 —— 其余的留在列表里继续等确认。
   *  一次拖 20 个文件时，可以先挑一个最急的跑起来，不必全等。 */
  async function startStagedOne(item) {
    try {
      let id = item.jobId;
      if (!id) {
        const r = await postJSON('/api/jobs',
          Object.assign({}, jobOptions(), { path: item.path,
                                             engine: item.engine || state.settings.engine }));
        id = (r.jobs && r.jobs[0]) ? r.jobs[0].id : '';
      }
      if (!id) throw new Error('任务创建失败');
      await postJSON('/api/jobs/start', { ids: [id] });
      state.staging = state.staging.filter(x => x.key !== item.key);
      state.current = null;      // 清掉当前展示，让自动跟随切到刚启动的这个任务
      state.segs = [];
      renderStaging();
      await refreshJobs();
      startJobPolling();
      toast(`已开始：${item.name}`, 'ok');
    } catch (e) {
      toast(`无法开始「${item.name}」：${e.message}`, 'bad');
      renderStaging();      // 恢复按钮可用状态
    }
  }

  /* ═════════════ 导出目录（工作台首页） ═════════════ */
  let commonDirs = {};
  let exportDirValue = '';        // '' = 默认目录；'@source_dir' = 与源文件同目录
  let dirCheckTimer = null;

  const DIR_PLACEHOLDER = '留空则用默认目录（soundscribe\\data\\exports）';
  const DIR_PLACEHOLDER_SRC = '与源文件同目录（每个文件导出到它自己所在的文件夹）';

  async function loadExportDir() {
    try { commonDirs = await api('/api/common-dirs'); } catch (_) { commonDirs = {}; }
    const items = [
      ['', '默认目录', 'soundscribe\\data\\exports'],
      ['@source_dir', '与源文件同目录', '导出到每个源视频所在的文件夹'],
      ['desktop', '桌面', commonDirs.desktop],
      ['documents', '文档', commonDirs.documents],
      ['downloads', '下载', commonDirs.downloads],
      ['home', '用户目录', commonDirs.home],
    ].filter(([v, , path]) => v === '' || v === '@source_dir' || path);

    $('#quickDirs').innerHTML = items.map(([v, label, path]) =>
      `<button class="chip-btn" data-dir="${esc(v)}" title="${esc(path || '')}">${esc(label)}</button>`
    ).join('');
    $$('#quickDirs .chip-btn').forEach(b => b.addEventListener('click', () => {
      const v = b.dataset.dir;
      setExportDir(v === '' || v === '@source_dir' ? v : (commonDirs[v] || ''));
    }));

    let saved = '';
    try { saved = localStorage.getItem('sb.exportDir') || ''; } catch (_) {}
    setExportDir(saved, true);
    verifyExportDir();
  }

  function setExportDir(v, silent) {
    exportDirValue = v || '';
    const inp = $('#exportDir');
    if (exportDirValue === '@source_dir') {
      inp.value = '';
      inp.placeholder = DIR_PLACEHOLDER_SRC;
    } else {
      inp.value = exportDirValue;
      inp.placeholder = DIR_PLACEHOLDER;
    }
    $$('#quickDirs .chip-btn').forEach(b => {
      const bv = b.dataset.dir;
      const on = (bv === '@source_dir' && exportDirValue === '@source_dir')
        || (bv === '' && exportDirValue === '')
        || (bv !== '' && bv !== '@source_dir' && commonDirs[bv] === exportDirValue);
      b.classList.toggle('on', on);
    });
    try { localStorage.setItem('sb.exportDir', exportDirValue); } catch (_) {}
    if (!silent) verifyExportDir();
  }

  async function verifyExportDir() {
    const note = $('#exportNote');
    if (exportDirValue === '@source_dir') {
      note.textContent = '每个文件导出到它自己所在的文件夹';
      note.className = 'note';
      return;
    }
    try {
      const r = await postJSON('/api/check-dir', { path: exportDirValue });
      if (!r.ok) {
        note.textContent = r.message;
        note.className = 'note bad';
      } else if (r.exists === false) {
        // 目录还不存在不是错误，如实告知即可
        note.textContent = r.message;
        note.className = 'note';
      } else {
        note.textContent = `将导出到：${r.path}` + (r.free_gb ? `　剩余 ${r.free_gb} GB` : '');
        note.className = 'note ok';
      }
    } catch (e) {
      note.textContent = '无法校验目录：' + e.message;
      note.className = 'note bad';
    }
  }

  $('#exportDir').addEventListener('input', e => {
    const v = e.target.value.trim();
    state.tmpDir = v;
    clearTimeout(dirCheckTimer);
    dirCheckTimer = setTimeout(() => setExportDir(v), 600);   // 输入停下再校验，别每敲一个字就发请求
  });

  $('#btnResetDir').addEventListener('click', () => { setExportDir(''); toast('已恢复默认导出目录'); });

  /* ── 应用内目录浏览器 ──
     不用系统「选择文件夹」对话框：独立进程弹出的窗口没有 owner，
     会跑到浏览器后面，用户以为按钮失效；中文路径还可能因编码问题传丢。 */
  let dirCur = { path: '', parent: '', writable: true };
  let dirRoots = null;

  function setDirStatus(msg, kind) {
    const el = $('#dirStatus');
    el.textContent = msg;
    el.className = 'modal-status' + (kind ? ' ' + kind : '');
  }

  async function openDirPicker() {
    $('#dirModal').classList.add('on');
    if (!dirRoots) {
      try { dirRoots = await api('/api/fs/roots'); }
      catch (_) { dirRoots = { drives: [], quick: [], home: '' }; }
      renderDirRoots();
    }
    const start = (exportDirValue && exportDirValue !== '@source_dir')
      ? exportDirValue : (dirRoots.home || '');
    loadDir(start);
  }

  function closeDirPicker() { $('#dirModal').classList.remove('on'); }

  function renderDirRoots() {
    const r = dirRoots || {};
    const parts = [];
    if ((r.quick || []).length) {
      parts.push('<div class="ghead" style="margin:4px 0 6px">常用位置</div>');
      parts.push(r.quick.map(q =>
        `<div class="dir-item" data-path="${esc(q.path)}"><span class="ic">◆</span><span class="nm">${esc(q.name)}</span></div>`
      ).join(''));
    }
    if ((r.drives || []).length) {
      parts.push('<div class="ghead" style="margin:14px 0 6px">此电脑</div>');
      parts.push(r.drives.map(d =>
        `<div class="dir-item" data-path="${esc(d.path)}"><span class="ic">▣</span><span class="nm">${esc(d.name)}</span></div>`
      ).join(''));
    }
    $('#dirRoots').innerHTML = parts.join('') || '<p class="dir-empty">读取不到磁盘列表</p>';
    $$('#dirRoots .dir-item').forEach(el =>
      el.addEventListener('click', () => loadDir(el.dataset.path)));
  }

  async function loadDir(path) {
    if (!path) return;
    $('#dirList').innerHTML = '<p class="dir-empty">读取中…</p>';
    let r;
    try { r = await api('/api/fs/list?path=' + encodeURIComponent(path)); }
    catch (e) { r = { ok: false, message: e.message }; }

    if (!r.ok) {
      $('#dirPathInput').value = path;
      $('#dirList').innerHTML = `<p class="dir-empty">${esc(r.message || '无法打开这个文件夹')}</p>`;
      setDirStatus(r.message || '', 'bad');
      return;
    }

    dirCur = { path: r.path, parent: r.parent, writable: r.writable };
    $('#dirPathInput').value = r.path;
    $('#dirUp').disabled = !r.parent;
    $$('#dirRoots .dir-item').forEach(el =>
      el.classList.toggle('on', el.dataset.path === r.path));

    const rows = (r.dirs || []).map(d =>
      `<div class="dir-row" data-path="${esc(d.path)}"><span class="ic">▸</span><span class="nm">${esc(d.name)}</span></div>`);
    $('#dirList').innerHTML = rows.length
      ? rows.join('') + (r.truncated ? '<p class="dir-empty">（文件夹太多，只列出了前 400 个）</p>' : '')
      : '<p class="dir-empty">这个文件夹里没有子文件夹<br>可以直接点右下角「使用这个文件夹」</p>';
    $$('#dirList .dir-row').forEach(el =>
      el.addEventListener('click', () => loadDir(el.dataset.path)));

    setDirStatus(r.writable ? '位置：' + r.path : (r.writable_message || '这个文件夹不可写'),
      r.writable ? '' : 'bad');
  }

  $('#btnPickDir').addEventListener('click', openDirPicker);
  $('#dirClose').addEventListener('click', closeDirPicker);
  $('#dirCancel').addEventListener('click', closeDirPicker);
  $('#dirModal').addEventListener('click', e => { if (e.target.id === 'dirModal') closeDirPicker(); });
  addEventListener('keydown', e => {
    if (e.key === 'Escape' && $('#dirModal').classList.contains('on')) closeDirPicker();
  });
  $('#dirUp').addEventListener('click', () => { if (dirCur.parent) loadDir(dirCur.parent); });
  $('#dirGo').addEventListener('click', () => loadDir($('#dirPathInput').value.trim()));
  $('#dirPathInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') loadDir(e.target.value.trim());
  });
  $('#dirUse').addEventListener('click', () => {
    if (!dirCur.path) { toast('还没有选好文件夹', 'bad'); return; }
    setExportDir(dirCur.path);
    closeDirPicker();
    toast('导出目录已更新');
  });

  /* ═════════════ 任务 ═════════════ */
  function jobOptions() {
    const s = state.settings;
    return {
      formats: s.formats,
      export_dir: exportDirValue,          // '' = 后端用默认目录；'@source_dir' = 与源文件同目录
      export_layout: s.layout,
      name_template: s.template,
      with_timestamps: s.timeline !== 'none',
      vad: s.vad === 1,
      normalize: s.normalize === 1,
      batched: true,
      engine: s.engine,
    };
  }

  function renderStages(cur) {
    const idx = STAGES.findIndex(s => s[0] === cur);
    $('#jobStages').innerHTML = STAGES.map((s, i) =>
      `<span class="stage ${i < idx ? 'done' : (i === idx ? 'now' : '')}">${esc(s[1])}</span>`
    ).join('<span class="sep">/</span>');
  }

  function subscribe(jobId) {
    if (state.es) { state.es.close(); state.es = null; }
    const es = new EventSource(`/api/jobs/${jobId}/events`);
    state.es = es;
    es.onmessage = ev => {
      let d; try { d = JSON.parse(ev.data); } catch (_) { return; }
      handleEvent(d);
    };
    es.onerror = () => { /* 浏览器自动重连；任务结束后服务端会关闭 */ };
  }

  let _lastJobStatus = '';
  function setCtl(status) {
    // ★ 只在「状态发生变化」时刷新系统状态：任务结束后导出文件、
    //   临时缓存都会变，侧边栏的占用条应该跟着动。
    //   不能用"每次都刷"——poll 每两秒调一次，会把请求打得太密。
    if (status !== _lastJobStatus) {
      const wasActive = ['running', 'paused', 'queued', 'pending'].includes(_lastJobStatus);
      _lastJobStatus = status;
      if (wasActive && !['running', 'paused', 'queued', 'pending'].includes(status)) {
        loadStorage();
      }
    }
    const p = $('#btnPause'), c = $('#btnCancel');
    if (status === 'running') {
      p.disabled = false; p.textContent = '暂停';
      c.disabled = false;
    } else if (status === 'paused') {
      p.disabled = false; p.textContent = '继续';
      c.disabled = false;
    } else {
      p.disabled = true; p.textContent = '暂停';
      c.disabled = true;
    }
  }

  let _jobCardScrolled = false;

  function scrollJobCardIntoView() {
    // ★ 只在"开始一个任务"时滚一次。之后不再自动滚 ——
    //   用户可能正在看下面的逐句稿，不能一直把页面拽回去。
    if (_jobCardScrolled) return;
    const card = $('#jobCard');
    if (!card || card.style.display === 'none') return;
    _jobCardScrolled = true;
    try {
      card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } catch (_) {
      card.scrollIntoView();
    }
  }

  function showJobCard(j) {
    $('#jobCard').style.display = '';
    $('#jobName').textContent = (j.media && j.media.name) || basename(j.source);
    const m = j.media || {};
    $('#jobMeta').textContent = m.name ? [m.duration_display, m.size_display, m.resolution || m.audio_codec].filter(Boolean).join(' · ') : '';
    renderJobReceipt(j.applied);
  }

  function handleEvent(d) {
    switch (d.type) {
      case 'snapshot': {
        const j = d.job;
        state.current = j;
        showJobCard(j);
        if (j.stage) renderStages(j.stage);
        $('#jobBar').style.width = (j.status === 'done' ? 100 : (j.pct || 0)) + '%';
        $('#jobStats').textContent = j.status === 'done' ? '已完成' : (j.stage_label || '排队中…');
        setCtl(j.status);
        if (j.route && Object.keys(j.route).length) renderRoute(j.route);
        if (j.segments && j.segments.length && !state.segs.length) {
          state.segs = j.segments.slice();
          $('#transcriptCard').style.display = '';
          renderTranscript();
        }
        break;
      }
      case 'queued':
        $('#jobStats').textContent = '排队中…'; setCtl('queued');
        break;
      case 'media':
        if (d.media) {
          $('#jobName').textContent = d.media.name || $('#jobName').textContent;
          $('#jobMeta').textContent = [d.media.duration_display, d.media.size_display, d.media.resolution || d.media.audio_codec].filter(Boolean).join(' · ');
        }
        break;
      case 'stage':
        renderStages(d.stage);
        setLiveStage(d.label || '准备中…');
        // 阶段切换也要更新进度条与文字：语言路由阶段要加载模型试跑采样，
        // 可能停留十几秒，只靠 progress 事件会让用户一直看到上一步的提示。
        if (typeof d.pct === 'number') $('#jobBar').style.width = Math.min(100, d.pct) + '%';
        if (d.label) {
          $('#jobStats').textContent = d.label
            + (typeof d.pct === 'number' ? '　' + Math.round(d.pct) + '%' : '');
        }
        break;
      case 'progress':
        if (typeof d.pct === 'number') $('#jobBar').style.width = Math.min(100, d.pct) + '%';
        $('#jobStats').textContent = progressText(d);
        break;
      case 'route':
        renderRoute(d.route); break;
      case 'segment':
        state.segs.push(d.segment);
        appendSegment(d.segment);
        $('#txCount').textContent = state.segs.length;
        $('#txChars').textContent = state.segs.reduce((a, s) => a + (s.text || '').length, 0);
    renderReviewCount();
        break;
      case 'post':
        if (d.post && d.post.correction_total) toast(`纠错表命中 ${d.post.correction_total} 处并已自动修正`, 'ok');
        break;
      case 'warning':
        // ★ "某些设置对它不生效"不是错误，不该弹红色 toast 吓人 ——
        //   它是一条**如实告知**，进提示条即可，同时回执里已经列清。
        if (d.kind === 'ignored_settings' || d.kind === 'hotword_dropped') {
          showBanner('warn', d.message);
        } else {
          showBanner(d.kind, d.message, d.suggestion);
          toast(d.message, 'bad');
        }
        break;
      case 'paused':
        setCtl('paused');
        if (state.current) state.current.status = 'paused';
        $('#jobStats').textContent = '已暂停 · 已用 ' + fmtDur((state.current && state.current.elapsed) || 0) + '　点「继续」恢复';
        toast('任务已暂停，进度已保留');
        break;
      case 'resumed':
        setCtl('running');
        if (state.current) state.current.status = 'running';
        $('#jobStats').textContent = '已恢复，继续识别…';
        break;
      case 'done':
        setCtl('done');
        $('#jobBar').style.width = '100%';
        $('#jobStats').textContent = '已完成';
        {
          const w = $('#jobLiveWrap');
          if (w) w.classList.remove('on');     // 停止脉冲，文字留在最后一句
        }
        renderStages('export');
        toast('转写完成，已导出 ' + Object.keys(d.result.exports || {}).length + ' 个文件', 'ok');
        if (state.es) { state.es.close(); state.es = null; }
        showExportBanner(d.result);
        renderJobReceipt(d.result.applied);   // 完成后再画一次（含最终形态）
        refreshJobs();
        break;
      case 'error':
        setCtl('failed');
        $('#jobStats').textContent = '失败';
        showBanner('error', d.message);
        toast(d.message, 'bad');
        if (state.es) { state.es.close(); state.es = null; }
        refreshJobs();
        break;
      case 'canceled':
        setCtl('canceled');
        $('#jobStats').textContent = '已取消';
        if (state.es) { state.es.close(); state.es = null; }
        refreshJobs();
        break;
    }
  }

  /** 还没出识别文字时，用当前阶段名填充实时行 ——
   *  否则"检测媒体/抽音轨/语言路由"那十几秒里那行字是静止的，又会让人怀疑卡住了。 */
  function setLiveStage(label) {
    if (liveBuffer) return;                 // 已经有识别文字了就不覆盖
    const el = $('#jobLive');
    const wrap = $('#jobLiveWrap');
    if (!el || !wrap) return;
    el.textContent = label || '准备中…';
    el.className = 'livetext idle';
  }

  function progressText(d) {
    const parts = [];
    if (d.pct != null) parts.push(Math.round(d.pct) + '%');
    if (d.detail) parts.push(d.detail);
    if (d.elapsed != null) parts.push('已用 ' + fmtDur(d.elapsed));
    if (d.eta_seconds) parts.push('剩余约 ' + fmtDur(d.eta_seconds));
    if (d.speed) parts.push(d.speed.toFixed(1) + '× 倍速');
    return parts.join('　');
  }

  /** 引擎 id → 显示名。★ 从能力矩阵的 engines 映射取，不在界面里写死。
   *
   *  这里原来是一句 `r.engine === 'sensevoice' ? 'SenseVoice' : 'Whisper'` ——
   *  于是路由到 Parakeet 时，路由卡片上**显示的是"Whisper"**。
   *  用户看到的引擎名和实际跑的不是同一个，这比不显示更糟。 */
  function engineName(id) {
    if (!id) return '—';
    const m = (state.caps && state.caps.engines) || {};
    if (m[id]) return m[id];
    const lbl = ENGINE_LABEL[id];
    return (lbl && lbl.seg) || id;
  }

  function renderRoute(r) {
    if (!r) return;
    $('#routeCard').style.display = '';
    // ★ 采样方式变了（从"看前 60 秒"改成"分散 N 处采样"），文案跟着改 ——
    //   写死"前段采样"会让人以为还是只看开头。
    const wins = r.sample_windows || [];
    $('#routeHint').textContent = r.manual
      ? '用户手动指定'
      : (r.sufficient === false ? '样本不足 · 用通用引擎兜底'
                                : `${wins.length || 1} 处分散采样判定`);
    $('#routeMetrics').innerHTML = [
      ['中文占比', (r.cjk_ratio_pct != null ? r.cjk_ratio_pct : 0) + '%', r.cjk_ratio > 0.85 ? 'accent' : ''],
      ['中文字符', r.cjk_chars, ''],
      ['英文词', r.latin_words, ''],
      ['选用引擎', engineName(r.engine), 'accent'],
    ].map(([k, v, c]) => `<div class="metric"><p class="k">${k}</p><p class="v ${c}">${esc(v)}</p></div>`).join('');

    // ★ 采样不足时必须说出来 —— 那种情况下引擎是"兜底选的"，不是"判出来的"，
    //   两件事在界面上混淆，用户就会以为自己素材的语言被识别清楚了。
    const flag = r.sufficient === false ? '★ 样本不足，未对语言下结论 → ' : '';
    const where = wins.length ? `　采样位置：${wins.join('、')}` : '';
    $('#routeReason').textContent = flag + (r.reason || '') + where;
    if (r.suggest_dual) showBanner('dual', '建议开启双引擎比对', r.suggest_reason);
  }

  /* ═════════════ 实时识别文字（进度条下方滚动那一行）═════════════
   * ★ 为什么值得做：CPU 识别再慢也远快于人眼读一行字。
   *   把刚识别出的内容立刻滚出来，用户的感受从"进度条卡住了"
   *   变成"它一直在读"，等待焦虑显著下降 —— 这不是装饰。
   *
   * 显示策略：只保留最近一段的尾部（新字从右侧出现、省略号留在左边），
   * 这样任何时刻看到的都是"最新读出来的那几个字"。
   */
  const LIVE_TAIL = 46;              // 一行最多显示多少个字
  let liveBuffer = '';

  function updateJobLive(text, reset) {
    const wrap = $('#jobLiveWrap');
    const el = $('#jobLive');
    if (!el || !wrap) return;
    if (reset) {
      liveBuffer = '';
      wrap.classList.remove('on');
      el.textContent = '准备中…';
      el.className = 'livetext idle';
      return;
    }
    const t = String(text || '').replace(/\s+/g, ' ').trim();
    if (!t) return;
    liveBuffer = (liveBuffer + t).slice(-LIVE_TAIL * 2);
    el.textContent = liveBuffer.slice(-LIVE_TAIL);
    el.className = 'livetext';
    wrap.classList.add('on');
    // 重新触发淡入动画（移除类 → 强制重排 → 加回）
    void el.offsetWidth;
    el.classList.add('fresh');
  }

  /** 更新"待复核"计数（后端只返回索引列表时也要能算出来） */
  function renderReviewCount() {
    const wrap = $('#txReviewWrap');
    if (!wrap) return;
    const n = state.segs.filter(s => s && s.needs_review).length;
    $('#txReview').textContent = n;
    wrap.style.display = n ? '' : 'none';
  }

  function appendSegment(s) {
    updateJobLive(s && s.text);          // 每来一句就刷新那一行
    const box = $('#transcript');
    const el = document.createElement('div');
    // ★ 三种段级标记各自独立，可以叠加：
    //   low      低置信（引擎自己没把握）
    //   corrected 被纠错表改过（已自动修正）
    //   review   疑似重复退化（机器不敢替用户决定，标出来供人工复核）
    el.className = 'tline'
      + (s.low_confidence ? ' low' : '')
      + (s.corrected ? ' corrected' : '')
      + (s.needs_review ? ' review' : '');
    const tip = s.review_reason ? ` title="待复核：${esc(s.review_reason)}"` : '';
    el.innerHTML = `<span class="ts"${tip}>${fmtDur(s.start)}</span>`
      + `<span class="tx">${esc(s.text)}</span>`;
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
  }

  function renderTranscript() {
    const box = $('#transcript');
    box.innerHTML = '';
    state.segs.forEach(appendSegment);
    $('#txCount').textContent = state.segs.length;
    $('#txChars').textContent = state.segs.reduce((a, s) => a + (s.text || '').length, 0);
    renderReviewCount();
  }

  function showBanner(kind, msg, suggestion) {
    const cls = (kind === 'error' || kind === 'language_drift') ? 'bad' : 'warn';
    const title = kind === 'language_drift' ? '语言一致性校验未通过'
      : kind === 'dual' ? '建议开启双引擎比对'
      : kind === 'error' ? '任务失败'
      : kind === 'hallucination' ? '已过滤疑似幻觉段'
      : '提示';
    const html = `<div class="banner ${cls}"><div class="ico"></div><div class="body">
      <strong>${esc(title)}</strong><p>${esc(msg)}${suggestion ? '<br>' + esc(suggestion) : ''}</p></div></div>`;
    const box = $('#wbBanner');
    if (kind === 'dual' || kind === 'hallucination') box.insertAdjacentHTML('beforeend', html);
    else box.innerHTML = html;
  }

  function showExportBanner(result) {
    const files = Object.entries(result.exports || {});
    if (!files.length) return;
    const dir = files[0][1].replace(/[\\/][^\\/]+$/, '');
    $('#wbBanner').insertAdjacentHTML('afterbegin', `<div class="banner"><div class="ico" style="background:var(--accent)"></div><div class="body">
      <strong>已导出 ${files.length} 个文件</strong>
      <p>${files.map(([k]) => esc(k.toUpperCase())).join(' · ')}<br><span style="font-family:ui-monospace,monospace;font-size:11px">${esc(dir)}</span></p>
      <div style="margin-top:9px"><button class="btn sm" data-open-folder="${esc(dir)}">打开文件夹</button></div>
    </div></div>`);
    $$('#wbBanner [data-open-folder]').forEach(b => b.addEventListener('click', () => openFolder(b.dataset.openFolder)));
  }
  async function openFolder(p) {
    try { await postJSON('/api/open-folder', { path: p }); }
    catch (e) { toast('无法打开目录：' + e.message, 'bad'); }
  }

  /* ── 暂停 / 继续 / 取消 ──
     ★ 点下去必须立刻有反馈：抽音轨（ffmpeg）与加载模型这些阶段是阻塞的，
       暂停要等它跑到下一个检查点才真正生效，通常几百毫秒到几秒。
       如果这时界面一动不动，用户就会以为按钮坏了 —— 实测被反馈过这个问题。 */
  $('#btnPause').addEventListener('click', async () => {
    const j = state.current;
    if (!j) return;
    const btn = $('#btnPause');
    const wasPaused = btn.textContent === '继续';
    btn.disabled = true;
    if (!wasPaused) {
      btn.textContent = '正在暂停…';
      $('#jobStats').textContent = '已请求暂停，当前步骤跑完就停（进度已保留）';
    } else {
      btn.textContent = '正在继续…';
    }
    try {
      const r = await postJSON(`/api/jobs/${j.id}/${wasPaused ? 'resume' : 'pause'}`);
      if (!wasPaused) {
        // 后端会告诉我们卡在哪个阶段 —— 那个阶段结束前暂停不会生效
        const where = (r && r.stage_label) ? r.stage_label : '当前步骤';
        $('#jobStats').textContent = `已请求暂停，「${where}」跑完就停（进度已保留）`;
      }
      // 真正的状态以后端的 paused / resumed 事件为准，这里不抢先改按钮
    } catch (e) {
      toast(e.message, 'bad');
      btn.disabled = false;
      btn.textContent = wasPaused ? '继续' : '暂停';
    }
  });

  $('#btnCancel').addEventListener('click', async () => {
    const j = state.current;
    if (!j) return;
    if (!confirm('确定取消这个任务吗？已识别的部分不会保留。')) return;
    try { await postJSON(`/api/jobs/${j.id}/cancel`); toast('已请求取消'); }
    catch (e) { toast(e.message, 'bad'); }
  });

  /* ═════════════ 任务列表 / 轮询 ═════════════ */
  let lastListSig = '';
  async function refreshJobs() {
    try {
      const r = await api('/api/jobs');
      state.jobs = r.jobs || [];
      $('#libCount').textContent = state.jobs.filter(j => j.status === 'done').length;
      $('#libCount').style.display = state.jobs.filter(j => j.status === 'done').length ? '' : 'none';

      const sig = state.jobs.map(j => j.id + j.status + j.pct + j.segments_count).join('|');
      if (sig !== lastListSig) { lastListSig = sig; renderJobLists(); }

      // 自动跟随：只在用户没有正在查看某条任务时才切换。
      // ★ 必须把「已完成 / 失败 / 已取消」也算进来 —— 否则任务在后台跑完后
      //   界面一片空白，用户会以为根本没跑。实测被反馈过类似问题。
      if (!state.current) {
        const next =
          state.jobs.find(j => ['running', 'paused', 'queued'].includes(j.status))
          || state.jobs.find(j => ['done', 'failed', 'canceled'].includes(j.status));
        if (next) await followJob(next.id);
      } else if (!state.jobs.some(j => j.id === state.current.id)) {
        // 正在查看的那条被删掉了 → 清空展示区
        state.current = null;
        state.segs = [];
        ['#jobCard', '#transcriptCard', '#routeCard'].forEach(s => { $(s).style.display = 'none'; });
      }
      return r;
    } catch (_) { return null; }
  }

  function startJobPolling() {
    if (state.jobTimer) return;
    state.jobTimer = setInterval(async () => {
      const r = await refreshJobs();
      const hasActive = state.jobs.some(j => ['running','paused','queued'].includes(j.status));
      if (!hasActive) { clearInterval(state.jobTimer); state.jobTimer = null; }
    }, 2500);
  }

  async function followJob(id) {
    try {
      const r = await api(`/api/jobs/${id}`);
      const j = r.job;
      state.current = j;
      state.segs = [];
      // ★ 切到一个"已经在跑/已完成"的任务时，实时行要立刻显示它最后识别出的内容，
      //   而不是停在"等待识别…"——否则用户以为任务没在动。
      {
        const segs = j.segments || [];
        if (segs.length) {
          liveBuffer = '';
          updateJobLive(segs.slice(-4).map(x => x.text || '').join(''));
          const w = $('#jobLiveWrap');
          if (w && j.status !== 'running' && j.status !== 'paused') w.classList.remove('on');
        } else {
          updateJobLive('', true);
        }
      }
      $('#transcriptCard').style.display = (j.segments && j.segments.length) ? '' : 'none';
      $('#jobCard').style.display = '';
      showJobCard(j);
      if (j.status === 'running') {
        _jobCardScrolled = false;
        setTimeout(scrollJobCardIntoView, 700);
      }
      renderStages(j.status === 'done' ? 'export' : (j.stage || 'probe'));
      $('#jobBar').style.width = (j.status === 'done' ? 100 : (j.pct || 0)) + '%';
      $('#jobStats').textContent = j.status === 'done' ? '已完成' : (j.stage_label || '排队中…');
      setCtl(j.status);
      if (j.route && Object.keys(j.route).length) renderRoute(j.route);
      if (j.segments && j.segments.length) { state.segs = j.segments.slice(); renderTranscript(); }
      if (['running','paused','queued'].includes(j.status)) subscribe(id);
    } catch (_) {}
  }

  function statusPill(s) {
    const m = {
      pending: ['warn', '待开始'], queued: ['', '排队中'], running: ['live', '进行中'],
      paused: ['warn', '已暂停'], done: ['', '已完成'], failed: ['bad', '失败'], canceled: ['', '已取消'],
    };
    const [c, t] = m[s] || ['', s];
    return `<span class="pill ${c}">${t}</span>`;
  }

  /* ═════════════ 列表：多选与批量清除 ═════════════ */

  /** 任务列表实际显示的内容（已在「待开始」面板里的那批不重复显示） */
  function visibleJobs() {
    const staged = new Set(state.staging.map(s => s.jobId).filter(Boolean));
    return state.jobs.filter(j => j.status !== 'pending' || !staged.has(j.id));
  }

  /** 丢掉已经不存在的选中项，避免删完之后计数虚高 */
  function prunePicked(list) {
    const ids = new Set(list.map(j => j.id));
    [...state.picked].forEach(id => { if (!ids.has(id)) state.picked.delete(id); });
  }

  /** 同步两个列表的操作条状态（选中数、全选态、按钮可用性） */
  function updateListBar() {
    const list = visibleJobs();
    prunePicked(list);
    const n = state.picked.size;
    $('#jobListBar').style.display = list.length ? '' : 'none';
    $('#jobPicked').textContent = n ? `已选 ${n} 项` : '未选中';
    $('#jobPicked').classList.toggle('on', n > 0);
    $('#btnDeletePicked').disabled = n === 0;
    const all = $('#pickAllJobs');
    all.checked = list.length > 0 && n === list.length;
    all.indeterminate = n > 0 && n < list.length;

    const lib = state.jobs.filter(j => j.status === 'done');
    const ln = lib.filter(j => state.picked.has(j.id)).length;
    $('#libListBar').style.display = lib.length ? '' : 'none';
    $('#libPicked').textContent = ln ? `已选 ${ln} 项` : '未选中';
    $('#libPicked').classList.toggle('on', ln > 0);
    $('#btnDeletePickedLib').disabled = ln === 0;
    const la = $('#pickAllLib');
    la.checked = lib.length > 0 && ln === lib.length;
    la.indeterminate = ln > 0 && ln < lib.length;
  }

  function pickBox(id) {
    const on = state.picked.has(id) ? ' checked' : '';
    return `<label class="pickbox" title="选中后可批量操作"><input type="checkbox" data-pick="${id}"${on}></label>`;
  }

  function bindPicks(root) {
    $$('[data-pick]', root).forEach(cb => cb.addEventListener('change', () => {
      if (cb.checked) state.picked.add(cb.dataset.pick);
      else state.picked.delete(cb.dataset.pick);
      const row = cb.closest('.jrow');
      if (row) row.classList.toggle('picked', cb.checked);
      updateListBar();     // 只更新操作条与高亮，不整表重绘，避免闪烁
    }));
  }

  /** 批量删除任务记录。执行中的会被跳过（后端也会拒绝），返回统计。 */
  async function bulkDelete(ids) {
    const stuck = [], go = [];
    ids.forEach(id => {
      const j = state.jobs.find(x => x.id === id);
      if (j && ['running', 'paused'].includes(j.status)) stuck.push(id);
      else go.push(id);
    });
    let n = 0, fail = 0;
    for (const id of go) {
      try { await api(`/api/jobs/${id}`, { method: 'DELETE' }); n++; }
      catch (_) { fail++; }
    }
    // 删掉的正是当前正在看的任务时，清空展示区
    if (state.current && go.includes(state.current.id)) {
      state.current = null;
      state.segs = [];
      ['#jobCard', '#transcriptCard', '#routeCard'].forEach(s => { $(s).style.display = 'none'; });
    }
    state.picked.clear();
    return { n, fail, stuck: stuck.length };
  }

  function renderJobLists() {
    const box = $('#recentJobs');
    const list = visibleJobs();
    if (!list.length) { box.innerHTML = '<p class="empty">还没有任务</p>'; }
    else {
      box.innerHTML = list.slice(0, 20).map(j => {
        const acts = [];
        if (j.status === 'pending') {
          acts.push(`<button class="btn sm primary" data-start="${j.id}">开始</button>`);
          acts.push(`<button class="btn sm ghost" data-drop="${j.id}">移除</button>`);
        } else if (j.status === 'running') {
          acts.push(`<button class="btn sm" data-pause="${j.id}">暂停</button>`);
          acts.push(`<button class="btn sm ghost" data-cancel="${j.id}">取消</button>`);
        } else if (j.status === 'paused') {
          acts.push(`<button class="btn sm" data-resume="${j.id}">继续</button>`);
          acts.push(`<button class="btn sm ghost" data-cancel="${j.id}">取消</button>`);
        } else {
          acts.push(`<button class="btn sm ghost" data-drop="${j.id}">移除</button>`);
        }
        const m = j.media || {};
        const meta = j.status === 'pending'
          ? [m.duration_display, m.size_display].filter(Boolean).join(' · ') || '等待开始'
          : `${fmtDur(j.duration)} · ${j.segments_count} 段 · ${j.chars} 字 · ${j.speed}×${j.engine_display ? ' · ' + j.engine_display : ''}`;
        return `<div class="jrow${state.picked.has(j.id) ? ' picked' : ''}" data-id="${j.id}">
          ${pickBox(j.id)}
          <div class="jinfo" data-open="${j.id}">
            <p class="jname">${esc(basename(j.source))}</p>
            <p class="jmeta">${esc(meta)}</p>
          </div>
          ${statusPill(j.status)}
          <span class="ctl">${acts.join('')}</span>
        </div>`;
      }).join('');
      bindPicks(box);
      $$('#recentJobs [data-open]').forEach(el => el.addEventListener('click', () => followJob(el.dataset.open)));
      $$('#recentJobs [data-start]').forEach(b => b.addEventListener('click', () => jobAction(b.dataset.start, 'start')));
      $$('#recentJobs [data-pause]').forEach(b => b.addEventListener('click', () => jobAction(b.dataset.pause, 'pause')));
      $$('#recentJobs [data-resume]').forEach(b => b.addEventListener('click', () => jobAction(b.dataset.resume, 'resume')));
      $$('#recentJobs [data-cancel]').forEach(b => b.addEventListener('click', () => jobAction(b.dataset.cancel, 'cancel')));
      $$('#recentJobs [data-drop]').forEach(b => b.addEventListener('click', () => jobAction(b.dataset.drop, 'drop')));
    }
    updateListBar();
    if ($('#view-library').classList.contains('on')) renderLibrary();
  }

  async function jobAction(id, act) {
    if (act === 'cancel' && !confirm('确定取消这个任务吗？')) return;
    try {
      if (act === 'drop') {
        try {
          await api(`/api/jobs/${id}`, { method: 'DELETE' });
          toast('已移除');
        } catch (e1) {
          // 409 = 还在执行。如果它其实已经卡住（进度长时间不动），
          // 允许强制摘掉这条记录，否则用户除了重启服务别无他法。
          if (!String(e1.message).includes('执行中')) throw e1;
          if (!confirm('这条任务还显示为「执行中」，通常不能移除。\n\n'
            + '如果它已经卡住不动了（进度长时间没变化），可以强制移除这条记录：\n'
            + '· 记录会从列表消失\n'
            + '· 后台线程可能仍在运行，但不再显示进度\n\n'
            + '要强制移除吗？')) return;
          await api(`/api/jobs/${id}?force=1`, { method: 'DELETE' });
          toast('已强制移除那条卡住的记录', 'ok');
        }
      } else {
        await postJSON(`/api/jobs/${id}/${act}`);
        const label = { start: '已开始', pause: '已暂停', resume: '已继续', cancel: '已请求取消' }[act];
        if (label) toast(label);
      }
      await refreshJobs();
      if (['start', 'resume'].includes(act)) startJobPolling();
    } catch (e) { toast(e.message, 'bad'); }
  }

  function renderLibrary() {
    const box = $('#libList');
    const done = state.jobs.filter(j => j.status === 'done');
    if (!done.length) {
      box.innerHTML = '<p class="empty">还没有完成的文稿</p>';
      updateListBar();
      return;
    }
    box.innerHTML = done.map(j => `
      <div class="jrow${state.picked.has(j.id) ? ' picked' : ''}" data-id="${j.id}">
        ${pickBox(j.id)}
        <div class="jinfo" data-open="${j.id}">
          <p class="jname">${esc(basename(j.source))}</p>
          <p class="jmeta">${fmtDur(j.duration)} · ${j.segments_count} 段 · ${j.chars} 字 · ${esc(j.finished_at || '')}</p>
        </div>
        <span class="pill">${Object.keys(j.exports || {}).length} 个导出</span>
      </div>`).join('');
    bindPicks(box);
    $$('#libList [data-open]').forEach(el => el.addEventListener('click', () => {
      go('workbench'); followJob(el.dataset.open);
    }));
    updateListBar();
  }

  /* ── 批量清除操作 ── */
  $('#pickAllJobs').addEventListener('change', e => {
    const list = visibleJobs();
    list.forEach(j => e.target.checked ? state.picked.add(j.id) : state.picked.delete(j.id));
    renderJobLists();
  });

  $('#btnDeletePicked').addEventListener('click', async () => {
    const ids = [...state.picked];
    if (!ids.length) return;
    if (!confirm(`删除选中的 ${ids.length} 条任务记录？\n\n· 已导出的转写文件不会被删除\n· 正在执行的任务会跳过`)) return;
    const r = await bulkDelete(ids);
    toast(`已删除 ${r.n} 条记录`
      + (r.fail ? `，${r.fail} 条失败` : '')
      + (r.stuck ? `，${r.stuck} 条执行中已跳过` : ''), r.n ? 'ok' : 'bad');
    await refreshJobs();
  });

  $('#btnClearFinished').addEventListener('click', async () => {
    const list = state.jobs.filter(j => ['done', 'failed', 'canceled'].includes(j.status));
    if (!list.length) { toast('没有已结束的任务记录', 'bad'); return; }
    if (!confirm(`清理 ${list.length} 条已结束的记录（完成 / 失败 / 已取消）？\n\n已导出的转写文件不会被删除。`)) return;
    const r = await bulkDelete(list.map(j => j.id));
    toast(`已清理 ${r.n} 条记录`, 'ok');
    await refreshJobs();
  });

  $('#btnClearAll').addEventListener('click', async () => {
    const all = state.jobs;
    if (!all.length) { toast('任务列表已经是空的', 'bad'); return; }
    const active = all.filter(j => ['running', 'paused'].includes(j.status));
    if (!confirm(`清空全部 ${all.length} 条任务记录？\n\n· 已导出的转写文件不会被删除`
      + (active.length ? `\n· 正在执行的 ${active.length} 条会保留` : ''))) return;
    const r = await bulkDelete(all.map(j => j.id));
    toast(`已清空 ${r.n} 条记录` + (r.stuck ? `，保留 ${r.stuck} 条执行中的` : ''), 'ok');
    await refreshJobs();
  });

  $('#pickAllLib').addEventListener('change', e => {
    const lib = state.jobs.filter(j => j.status === 'done');
    lib.forEach(j => e.target.checked ? state.picked.add(j.id) : state.picked.delete(j.id));
    renderLibrary();
    renderJobLists();
  });

  $('#btnDeletePickedLib').addEventListener('click', async () => {
    const lib = state.jobs.filter(j => j.status === 'done' && state.picked.has(j.id));
    if (!lib.length) return;
    if (!confirm(`从文稿库删除选中的 ${lib.length} 条记录？\n\n已导出的转写文件不会被删除。`)) return;
    const r = await bulkDelete(lib.map(j => j.id));
    toast(`已删除 ${r.n} 条记录`, 'ok');
    await refreshJobs();
  });

  $('#btnClearLib').addEventListener('click', async () => {
    const lib = state.jobs.filter(j => j.status === 'done');
    if (!lib.length) { toast('文稿库已经是空的', 'bad'); return; }
    if (!confirm(`清空文稿库的全部 ${lib.length} 条记录？\n\n已导出的转写文件不会被删除。`)) return;
    const r = await bulkDelete(lib.map(j => j.id));
    toast(`已清空 ${r.n} 条记录`, 'ok');
    await refreshJobs();
  });

  /* ═════════════ 设置面板 ═════════════ */
  /* ═════════════ 引擎选项：按「本机实际可用」动态显隐 ═════════════
   * ★ 之前这里是硬编码的 3 项（自动判定 / SenseVoice / Whisper），
   *   结果用户装了 Parakeet、Moonshine 也在模型中心显示"已安装"，
   *   但转写时**根本选不到** —— 装了却用不上，等于没装。
   *   现在统一按 /api/env 的 engines_ready（依赖装好 且 模型已下载）来显示。
   */
  // hint 就是下拉里括号里的那句"适合什么" —— 用户面对一串模型名时，
  // 真正需要的不是型号，而是"我这段素材该选哪个"。
  const ENGINE_LABEL = {
    auto:       { seg: '自动判定', hint: '按语言自动选', tag: '推荐' },
    sensevoice: { seg: 'SenseVoice', hint: '适合中文' },
    parakeet:   { seg: 'Parakeet', hint: '适合英文' },
    moonshine:  { seg: 'Moonshine', hint: '英文 · 体积最小' },
    whisper:    { seg: 'Whisper', hint: '通用 / 可翻译' },
  };
  const ENGINE_ORDER = ['auto', 'sensevoice', 'parakeet', 'moonshine', 'whisper'];

  function readyEngineIds() {
    const ready = ((state.env && state.env.engines_ready) || []).map(x => x.engine);
    // 至少给 Whisper，避免接口异常时下拉变空
    const list = ENGINE_ORDER.filter(e => e === 'auto' || ready.includes(e));
    return list.length > 1 ? list : ['auto', 'sensevoice', 'whisper'];
  }

  /** 引擎下拉（自绘，不用原生 <select>）。
   *
   *  ★ 为什么不用原生 select：弹层由系统绘制，配色/圆角/字体跟不了页面主题，
   *    深色主题下尤其突兀（白底黑字一坨）。这个页面"界面美观是硬指标"，
   *    所以自己画：触发器 + 浮层列表，样式与卡片同源。
   *
   *  ★ 生成逻辑只有这一处 —— 行渲染与列表同步都调它。
   *    之前两处各写一份，改一处必漏另一处（"引擎选不到"那个 bug 就是这么来的）。
   */
  function enginePickerHTML(cur) {
    const val = ENGINE_LABEL[cur] ? cur : 'auto';
    const meta = ENGINE_LABEL[val];
    const overridden = val !== 'auto';
    return `<div class="msel${overridden ? ' set' : ''}" data-act="engine" data-value="${val}"
                 role="combobox" tabindex="0" aria-haspopup="listbox" aria-expanded="false"
                 title="${overridden ? '这个文件已单独指定引擎；想恢复自动判定就选第一项' : '按素材语言自动选择识别引擎'}">
        <span class="mv">${meta.seg}<em>（${meta.hint}）</em></span>
        <svg class="mc" viewBox="0 0 10 6" aria-hidden="true">
          <path d="M1 1l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.4"
                stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <div class="mmenu" role="listbox">
          ${readyEngineIds().map(id => {
            const m = ENGINE_LABEL[id];
            // ★ 选项里直接标出"这台引擎上哪些设置不生效" —— 清单来自能力矩阵。
            //   让用户在**选之前**就知道代价，而不是跑完才被告知。
            const off = id === 'auto' ? [] : engineScope(id).off.map(x => x.c.label);
            return `<button type="button" class="mo${id === val ? ' on' : ''}" data-v="${id}"
                            role="option" aria-selected="${id === val}">
                      <span class="mn">${m.seg}<em>（${m.hint}）</em></span>
                      ${m.tag ? `<span class="mtag">${m.tag}</span>` : ''}
                      ${id === val ? '<span class="mtick">✓</span>' : ''}
                      ${off.length ? `<span class="mno">以下设置对它无效：${esc(off.join('、'))}</span>` : ''}
                    </button>`;
          }).join('')}
        </div>
      </div>`;
  }

  /** ★★ 展开浮层时，把承载它的那张卡片提上来。
   *
   *  为什么必须这么做（2026-09-30 Eli 报的 bug）：
   *    `.card` 上有 `backdrop-filter: blur(...)`，而 backdrop-filter 会
   *    **创建层叠上下文** —— 于是浮层的 `z-index: 40` 只在"自己这张卡片内部"
   *    有效，**突破不出去和兄弟卡片比**。后面那张卡片（转写选项）按 DOM 顺序
   *    天然盖在上面，把下拉菜单的下半截吃掉 → 靠后的模型（Moonshine / Whisper）
   *    根本点不到。现象像"被裁掉"，其实是"被盖住"。
   *
   *  ★ 上一轮修的是"浮层盖住下一行的下拉"，这一次是"浮层被下一张卡片盖住"——
   *    同一个 z-index 话题的两个方向。凡是自绘浮层，两个方向都要想到。
   */
  function liftCard(el, on) {
    const card = el && el.closest ? el.closest('.card') : null;
    if (card) card.classList.toggle('lift', !!on);
  }

  /** 给一个自绘下拉绑定交互。
   *  打开/选中/关闭 + 点外部关闭 + Esc 关闭 —— 原生 select 免费给的行为，
   *  自己画就得自己补全，否则"点开收不起来"是很常见的返工点。 */
  function bindEnginePicker(root, onChange) {
    const trigger = root;
    const menu = root.querySelector('.mmenu');
    const valueEl = root.querySelector('.mv');
    if (!menu) return;

    const close = () => {
      root.classList.remove('open');
      root.setAttribute('aria-expanded', 'false');
      liftCard(root, false);
    };
    const open = () => {
      // 同时只允许一个下拉展开，避免多个浮层叠在一起
      $$('#stageList .msel.open').forEach(x => {
        if (x !== root) { x.classList.remove('open'); liftCard(x, false); }
      });
      root.classList.add('open');
      root.setAttribute('aria-expanded', 'true');
      liftCard(root, true);          // ★ 把卡片提到兄弟之上，否则下半截被盖住
    };
    const setValue = v => {
      const meta = ENGINE_LABEL[v] || ENGINE_LABEL.auto;
      root.dataset.value = v;
      root.classList.toggle('set', v !== 'auto');
      valueEl.innerHTML = `${meta.seg}<em>（${meta.hint}）</em>`;
      root.title = v !== 'auto'
        ? '这个文件已单独指定引擎；想恢复自动判定就选第一项'
        : '按素材语言自动选择识别引擎';
      $$('.mo', menu).forEach(b => {
        const on = b.dataset.v === v;
        b.classList.toggle('on', on);
        b.setAttribute('aria-selected', String(on));
        const tick = $('.mtick', b);
        if (on && !tick) b.insertAdjacentHTML('beforeend', '<span class="mtick">✓</span>');
        if (!on && tick) tick.remove();
      });
      if (onChange) onChange(v);
    };

    trigger.addEventListener('click', e => {
      if (e.target.closest('.mmenu')) return;      // 点菜单项交给下面处理
      root.classList.contains('open') ? close() : open();
    });
    trigger.addEventListener('keydown', e => {
      if (e.key === 'Escape') { close(); return; }
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); root.classList.toggle('open'); }
    });
    $$('.mo', menu).forEach(btn => btn.addEventListener('click', e => {
      e.stopPropagation();
      setValue(btn.dataset.v);
      close();
    }));
    // 点页面其他地方关闭（用一次性监听，避免每个下拉都挂一个 document 监听）
    root._closePicker = close;
  }

  /** 关闭所有已展开的下拉 */
  function closeAllPickers() {
    $$('#stageList .msel.open').forEach(x => {
      x.classList.remove('open');
      x.setAttribute('aria-expanded', 'false');
      liftCard(x, false);            // ★ 卡片也要放回去，否则它会一直压着后面的卡片
    });
  }

  // 全局只需一份监听：点在**任何下拉组件的范围之外**才关闭。
  // ★ 放在 .msel 里的事件（触发器、浮层、选项）一律交给组件自己处理 ——
  //   这样"直接点下一行的下拉"能一步切换过去，不需要先点一下关掉再点一下打开。
  document.addEventListener('click', e => {
    if (!$$('#stageList .msel.open').length) return;
    if (e.target.closest('.msel')) return;   // 组件内部 → 它自己管
    closeAllPickers();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeAllPickers();
  });

  /** 重建所有引擎下拉（例如环境刚加载完、可用引擎列表变了）。
   *  ★ 引擎选择现在只在「待开始」列表里，转写选项里没有第二处了。 */
  function syncEngineOptions() {
    $$('#stageList [data-act="engine"]').forEach(el => {
      const cur = el.dataset.value || 'auto';
      const holder = document.createElement('div');
      holder.innerHTML = enginePickerHTML(cur);
      const fresh = holder.firstElementChild;
      el.replaceWith(fresh);
      const item = state.staging.find(x => x.key === fresh.closest('.srow')?.dataset.key);
      bindEnginePicker(fresh, v => { if (item) item.engine = v; });
    });
  }

  function segBind(sel, key, cast, after) {
    $$(sel + ' button').forEach(b => b.addEventListener('click', () => {
      if (b.disabled) return;
      $$(sel + ' button').forEach(x => x.classList.toggle('on', x === b));
      state.settings[key] = cast ? cast(b.dataset.v) : b.dataset.v;
      if (after) after();
    }));
  }
  segBind('#optTimeline', 'timeline', null, () => {
    const off = state.settings.timeline === 'none';
    ['srt', 'vtt'].forEach(f => {
      const btn = $(`#optFormats button[data-v="${f}"]`);
      if (off) { btn.classList.remove('on'); btn.disabled = true; }
      else { btn.disabled = false; }
    });
    state.settings.formats = $$('#optFormats button.on').map(b => b.dataset.v);
    $('#timelineNote').textContent = off
      ? '当前关闭时间轴 —— SRT / VTT 将无法导出，播放器也不能逐句高亮'
      : '段级时间戳：每句带起止时间，可导出字幕、可逐句高亮';
    updateFormatNote();
  });
  segBind('#optVad', 'vad', Number);
  segBind('#optNorm', 'normalize', Number);
  segBind('#optLayout', 'layout');
  $$('#optFormats button').forEach(b => b.addEventListener('click', () => {
    if (b.disabled) return;
    b.classList.toggle('on');
    state.settings.formats = $$('#optFormats button.on').map(x => x.dataset.v);
    updateFormatNote();
  }));
  $('#optTemplate').addEventListener('input', e => { state.settings.template = e.target.value; });

  function updateFormatNote() {
    const n = state.settings.formats.length;
    $('#formatNote').textContent = n ? `已选 ${n} 种格式，转写完成后自动导出` : '未选择任何格式，转写后不会自动导出';
  }

  /* ═════════════ 纠错规则表 ═════════════ */
  let corrCache = null;
  let corrFilterText = '';

  async function loadCorrections() {
    try {
      corrCache = await api('/api/corrections');
      state.corrections = corrCache;
      renderCorrections();
      // ★ 词库页那段"规则是在哪台引擎上总结的、现在还对不对得上"由数据算出，
      //   所以要在纠错表读回来之后重画一次生效范围卡片。
      renderCapabilityNotes();
    } catch (e) { toast('读取纠错表失败：' + e.message, 'bad'); }
  }

  /* ═════════════ 能力矩阵：生效范围的唯一来源 ═════════════
   ★★ 这一节的存在理由，写在 capabilities.py 的模块注释里，这里只说界面侧的规矩：
      **凡是"某个设置对某个引擎生不生效"的话，都不许手写。**
      手写的那份一定会和实际行为漂移，而且漂了没有任何机制能发现 ——
      实测漂过两次（"热词仅 Whisper 生效"而 Parakeet 已接上；
       "内置规则来自 Whisper"而这句话的事实来源当时根本没记录在数据里）。
      所以这里只做一件事：把 /api/capabilities 的输出排成界面。
   */
  function capList() { return (state.caps && state.caps.capabilities) || []; }
  function capById(id) { return capList().find(c => c.id === id) || null; }

  /** 某台引擎上"能生效 / 不生效"的可设项清单。 */
  function engineScope(engine) {
    const caps = capList().filter(c => c.scope === 'engine' && (c.by_engine || {})[engine]);
    const works = [], off = [];
    caps.forEach(c => {
      const s = c.by_engine[engine];
      (s.level === 'supported' ? works : off).push({ c, s });
    });
    return { works, off };
  }

  async function loadCapabilities() {
    try { state.caps = await api('/api/capabilities'); }
    catch (_) { state.caps = null; }
    renderCapabilityNotes();
    syncEngineOptions();
  }

  function scopeItem(c, extra) {
    if (!c) return '';
    const isAll = c.scope !== 'engine';
    const tag = isAll ? 'ok' : (c.hint_short || '').startsWith('暂') ? 'off'
                                                  : (c.hint_short || '').startsWith('仅') ? 'off' : 'ok';
    const rows = c.scope === 'engine' ? Object.keys(c.by_engine || {}).map(e => {
      const s = c.by_engine[e];
      const cls = s.level === 'supported' ? 'ok2' : 'no2';
      return `<span class="ecl ${cls}" title="${esc(s.why || '')}">${esc(s.name)}</span>`;
    }).join('') : '';
    return `<div class="scopeitem">
      <p class="scopetitle">${esc(c.label)} <span class="tag ${tag}">${esc(c.hint_short || '')}</span></p>
      <p class="scopenote">${esc(c.desc || '')}</p>
      ${rows ? `<p class="scopenote eclrow">${rows}</p>` : ''}
      <p class="scopenote">${esc(c.hint || '')}</p>
      ${c.ui_note ? `<p class="scopenote">${esc(c.ui_note)}</p>` : ''}
      ${extra || ''}
    </div>`;
  }

  function renderCapabilityNotes() {
    // ① 工作台的 VAD 说明
    const v = capById('vad');
    const vEl = $('#vadScopeNote');
    if (vEl) {
      vEl.innerHTML = v
        ? `<b>生效范围</b>：${esc(v.hint)}。${esc(v.ui_note || '')}`
        : '生效范围读取失败，请刷新页面重试。';
    }

    // ② 词库页"这些设置分别在什么时候起作用"
    const body = $('#scopeBody');
    if (!body) return;
    if (!capList().length) {
      body.innerHTML = '<p class="empty" style="padding:12px 0">生效范围读取失败，请刷新页面重试</p>';
      return;
    }
    const c1 = capById('corrections');
    const c2 = capById('hotwords');
    const cal = (state.corrections && state.corrections.calibration) || null;

    // ★ 标定来源那段提示由**数据**算出来（规则里记了 source_engine），不是手写的
    const calHtml = (cal && cal.warn)
      ? `<p class="scopenote warnline">★ ${esc(cal.note)}</p>` : '';

    body.innerHTML = `
      <div class="grid c2">
        ${scopeItem(c1, calHtml)}
        ${scopeItem(c2, null)}
      </div>
      <p class="note" style="margin-top:12px">
        所以：<b>想让某个词一定对，最可靠的做法是往纠错规则表里加一条</b>
        （${esc((c1 || {}).hint_short || '')}）；热词库只在引擎支持识别期提示词时才有用
        （${esc((c2 || {}).hint_short || '')}）。
      </p>`;
  }

  /* ═════════════ 设置回执：这一趟到底哪些设置生效了 ═════════════ */
  function renderJobReceipt(applied) {
    const box = $('#jobReceipt');
    if (!box) return;
    const ok = (applied && applied.applied) || [];
    const ig = (applied && applied.ignored) || [];
    const ad = (applied && applied.adapted) || [];

    // ★ 判断"有没有内容可显示"时，**必须把不生效项也算进去**。
    //   原来的条件是 `applied.applied.length` —— 于是 SenseVoice 任务
    //   （它对热词/VAD/批处理全部不支持，items 数为 0）**整块回执被隐藏**。
    //   而那恰恰是最需要看到回执的场景：用户往热词库填了一堆词，
    //   跑完中文转写后界面上什么都没说，他会以为热词生效了。
    //   "0 项生效 + 4 项不生效"本身就是最该显示的一句话。
    if (!ok.length && !ig.length) {
      box.style.display = 'none';
      return;
    }
    box.style.display = '';
    // ★★ 设备要显示出来，而且**请求值**与**实际值**不一致时要明确指出。
    //    Eli 就是因为在界面上看不到任何设备信息，才怀疑"是不是全跑在 CPU 上"。
    //    而"传了 cuda 但静默回落 CPU"恰恰是这类项目最容易发生的错觉 ——
    //    显示实际值、并在回落时说出来，才算如实。
    const dev = applied.device || '';
    const devLabel = { cuda: 'GPU · CUDA', coreml: 'GPU · CoreML',
                       directml: 'GPU · DirectML', cpu: 'CPU' }[dev] || dev;
    const req = applied.device_requested || dev;
    const fellBack = req && dev && req !== dev;
    const devHtml = devLabel
      ? ` · <span class="rctdev${fellBack ? ' warn' : ''}"${
          fellBack ? ` title="请求 ${esc(req)}，实际回落到 ${esc(dev)}"` : ''}>${esc(devLabel)}${
          fellBack ? '（回落）' : ''}</span>`
      : '';
    box.innerHTML = `
      <div class="rcthead">
        <span class="rcttitle">设置回执</span>
        <span class="hint">用 ${esc(applied.engine_display || applied.engine || '')}${devHtml}
          · ${ok.length} 项生效${ig.length ? ` · ${ig.length} 项对它不生效` : ''}</span>
      </div>
      <div class="rctrows">
        ${ok.map(a => `<span class="rctrow ok2" title="传成 ${esc(a.param)}（${esc(a.where)}）">
            ${esc(a.label)} <em>${esc(a.form || '')}</em></span>`).join('')}
        ${ig.map(i => `<span class="rctrow no2" title="${esc(i.why || '')}">
            ${esc(i.label)} <em>不生效</em></span>`).join('')}
      </div>
      ${ad.length ? `<p class="note" style="margin:8px 0 0">${ad.map(n => esc(n.message)).join('<br>')}</p>` : ''}`;
  }

  function renderCorrections() {
    const c = corrCache;
    if (!c) return;
    const kw = corrFilterText.trim().toLowerCase();
    const hit = r => !kw
      || String(r.pattern).toLowerCase().includes(kw)
      || String(r.replacement || '').toLowerCase().includes(kw);

    $('#corrCount').textContent =
      `${c.builtin_total} 条内置 · ${c.user_total} 条我的`
      + (c.hidden_total ? ` · 已移除 ${c.hidden_total} 条` : '')
      + ` · ${c.hallucination_patterns.length} 条幻觉黑名单`;

    const bAll = (c.builtin_rules || []);
    const uAll = (c.user_rules || []);
    const bRows = bAll.filter(hit).map(r => ruleRow(r, true)).join('');
    const uRows = uAll.filter(hit).map(r => ruleRow(r, false)).join('');

    const sections = [];
    if (bRows) {
      sections.push(`<div class="ghead">内置规则（${bAll.length} 条 · 可移除，随时能恢复）</div>
        <div class="rulebox">${bRows}</div>`);
    }
    if (uRows) {
      sections.push(`<div class="ghead">我的导入（${uAll.length} 条）</div>
        <div class="rulebox">${uRows}</div>`);
    } else if (!kw) {
      sections.push(`<div class="ghead">我的导入</div>
        <p class="empty" style="padding:14px 0">还没有导入过规则。点上方「导入规则文件」或手动添加。</p>`);
    }
    if (kw && !bRows && !uRows) {
      sections.length = 0;
      sections.push(`<p class="empty" style="padding:16px 0">没有匹配「${esc(corrFilterText)}」的规则</p>`);
    }
    $('#corrList').innerHTML = sections.join('');

    const fl = $('#corrFilterHint');
    if (fl) fl.textContent = kw ? `匹配 ${bRows.split('rrow').length - 1 + uRows.split('rrow').length - 1} 条` : '';

    $$('#corrList [data-del-rule]').forEach(b => b.addEventListener('click', async () => {
      if (!confirm('删除这条规则？')) return;
      try {
        await api(`/api/corrections/${b.dataset.delRule}`, { method: 'DELETE' });
        toast('已删除', 'ok');
        loadCorrections();
      } catch (e) { toast(e.message, 'bad'); }
    }));

    $$('#corrList [data-hide-rule]').forEach(b => b.addEventListener('click', async () => {
      const key = b.dataset.hideRule;
      if (!confirm(`把内置规则「${key}」移出？\n\n它只是被标记为不用，点「恢复内置规则」随时能找回来。\n（把软件交给别人时，可以先清掉这些个人录音留下的规则）`)) return;
      try {
        await postJSON('/api/builtin/hide', { kind: 'corrections', key });
        toast(`已移除内置规则「${key}」，可随时恢复`, 'ok');
        loadCorrections();
      } catch (e) { toast(e.message, 'bad'); }
    }));
  }

  function ruleRow(r, builtin) {
    return `<div class="rrow">
      <div class="pair"><span class="from">「${esc(r.pattern)}」</span>
        <span style="color:var(--text-3)">→</span>
        <span class="to">「${esc(r.replacement || '（删除）')}」</span>
        ${r.severity === 'high' ? '<span class="mini-pill" style="margin-left:8px;color:var(--accent)">高优先</span>' : ''}
        ${builtin ? '' : `<span class="src">${esc(r.source || '手动添加')}</span>`}
      </div>
      ${builtin
        ? `<span class="mini-pill">内置</span><span class="tool" data-hide-rule="${esc(r.pattern)}" title="不再使用这条内置规则（可恢复）">移除</span>`
        : `<span class="tool" data-del-rule="${r.id}">删除</span>`}
    </div>`;
  }

  $('#corrFilter').addEventListener('input', e => {
    corrFilterText = e.target.value;
    renderCorrections();
  });

  $('#btnCorrRestore').addEventListener('click', async () => {
    if (!corrCache || !corrCache.hidden_total) { toast('没有被移除的内置规则', 'bad'); return; }
    if (!confirm(`恢复 ${corrCache.hidden_total} 条被移除的内置规则？`)) return;
    try {
      await postJSON('/api/builtin/restore', { kind: 'corrections' });
      toast('已恢复全部内置规则', 'ok');
      loadCorrections();
    } catch (e) { toast(e.message, 'bad'); }
  });

  $('#btnCorrImport').addEventListener('click', () => $('#corrFile').click());
  $('#corrFile').addEventListener('change', async e => {
    const f = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!f) return;
    const note = $('#corrImportNote');
    note.textContent = '正在解析 ' + f.name + ' …';
    const fd = new FormData(); fd.append('file', f, f.name);
    try {
      const r = await api('/api/corrections/import', { method: 'POST', body: fd });
      const parts = [`识别为${r.format}`, `新增 ${r.added} 条规则`];
      if (r.skipped) parts.push(`跳过 ${r.skipped} 条（重复或无效）`);
      if (r.patterns_added) parts.push(`幻觉黑名单 +${r.patterns_added}`);
      note.textContent = parts.join(' · ');
      toast(`已从 ${f.name} 导入：${r.added} 条规则` + (r.skipped ? `，跳过 ${r.skipped} 条` : ''), r.added ? 'ok' : 'bad');
      loadCorrections();
    } catch (err) {
      note.textContent = '导入失败：' + err.message;
      toast('导入失败：' + err.message, 'bad');
    }
  });

  $('#btnCorrAddDo').addEventListener('click', async () => {
    const p = $('#corrPat').value.trim();
    const rp = $('#corrRep').value.trim();
    if (!p) { toast('请填写「错误写法」', 'bad'); return; }
    if (p === rp) { toast('错误写法与正确写法相同，无需添加', 'bad'); return; }
    try {
      const r = await postJSON('/api/corrections', { pattern: p, replacement: rp });
      if (r.added) {
        toast(rp ? `已添加：${p} → ${rp}` : `已添加：删除「${p}」`, 'ok');
        $('#corrPat').value = ''; $('#corrRep').value = '';
        loadCorrections();
      } else {
        toast(r.skipped ? '这条规则已存在' : '未能添加', 'bad');
      }
    } catch (e) { toast(e.message, 'bad'); }
  });

  $('#btnCorrClear').addEventListener('click', async () => {
    if (!confirm('清空你导入和手动添加的全部纠错规则？\n\n内置规则不受影响。')) return;
    try {
      const r = await postJSON('/api/corrections/clear-user');
      toast(`已清空 ${r.removed} 条个人规则`, 'ok');
      $('#corrImportNote').textContent = '';
      loadCorrections();
    } catch (e) { toast(e.message, 'bad'); }
  });

  $('#btnCorrTpl').addEventListener('click', () => downloadTemplate('corrections'));

  /* ═════════════ 热词库 ═════════════ */
  const SEP = '\u0000';        // 组合键分隔符：同一个词可能出现在不同分组里
  let hotCache = null;
  let hotFilterText = '';

  async function loadHotwords() {
    try {
      hotCache = await api('/api/hotwords');
      renderHotwords();
    } catch (e) { toast('读取热词库失败：' + e.message, 'bad'); }
  }

  function renderHotwords() {
    const h = hotCache;
    if (!h) return;
    const g = h.detail || {};
    const kw = hotFilterText.trim().toLowerCase();

    $('#hotCount').textContent =
      `${h.builtin_total || 0} 个内置 · ${h.user_total || 0} 个我的`
      + (h.hidden_total ? ` · 已移除 ${h.hidden_total} 个` : '')
      + ` · ${Object.keys(g).length} 个分组`;

    // 用「组名\0词」做键，避免同一个词出现在不同组时认错归属
    const userIds = {};
    (h.user_items || []).forEach(x => { userIds[(x.group || '') + SEP + x.term] = x.id; });

    let shown = 0;
    const html = Object.keys(g).sort().map(k => {
      const all = g[k] || [];
      const nameHit = !kw || k.toLowerCase().includes(kw);
      const words = nameHit ? all : all.filter(w => String(w).toLowerCase().includes(kw));
      if (!words.length) return '';
      shown += words.length;
      return `<div class="ghead">${esc(k)} <span style="color:var(--text-3)">${words.length} 个${nameHit ? '' : '（匹配）'}</span></div>
        <div class="itemlist">${words.map(w => {
          const id = userIds[k + SEP + w];
          return id
            ? `<span class="item user"><span class="txt">${esc(w)}</span><span class="x" data-del-word="${id}" title="删除">×</span></span>`
            : `<span class="item"><span class="txt">${esc(w)}</span><span class="x hide-builtin" data-hide-word="${esc(w)}" title="不再使用这个词（可恢复）">×</span></span>`;
        }).join('')}</div>`;
    }).join('');

    $('#hotList').innerHTML = shown
      ? html
      : `<p class="empty" style="padding:16px 0">${kw ? '没有匹配「' + esc(hotFilterText) + '」的热词' : '暂无热词'}</p>`;

    const batches = (h.batches || []).filter(b => b.kind === 'hotwords');
    $('#hotBatches').innerHTML = batches.length
      ? `<div class="ghead">我的导入批次（可整批撤销）</div>
         <div class="rulebox">${batches.map(b => `
           <div class="rrow"><div class="pair">
             <span>${esc(b.source)}</span>
             <span class="src">${esc(b.at)} · ${b.added} 个词</span>
           </div><span class="tool" data-del-batch="${b.id}">撤销</span></div>`).join('')}</div>`
      : '';

    $$('#hotList [data-del-word]').forEach(b => b.addEventListener('click', async () => {
      try { await api(`/api/hotwords/${b.dataset.delWord}`, { method: 'DELETE' }); loadHotwords(); }
      catch (e) { toast(e.message, 'bad'); }
    }));

    $$('#hotList [data-hide-word]').forEach(b => b.addEventListener('click', async () => {
      const key = b.dataset.hideWord;
      if (!confirm(`把内置热词「${key}」移出？\n\n它只是被标记为不用，点「恢复内置热词」随时能找回来。`)) return;
      try {
        await postJSON('/api/builtin/hide', { kind: 'hotwords', key });
        toast(`已移除「${key}」，可随时恢复`, 'ok');
        loadHotwords();
      } catch (e) { toast(e.message, 'bad'); }
    }));

    $$('#hotBatches [data-del-batch]').forEach(b => b.addEventListener('click', async () => {
      if (!confirm('撤销这一批导入？')) return;
      try {
        const r = await api(`/api/hotwords/batch/${b.dataset.delBatch}`, { method: 'DELETE' });
        toast(`已移除 ${r.removed} 个词`, 'ok');
        loadHotwords();
      } catch (e) { toast(e.message, 'bad'); }
    }));
  }

  $('#hotFilter').addEventListener('input', e => {
    hotFilterText = e.target.value;
    renderHotwords();
  });

  $('#btnHotRestore').addEventListener('click', async () => {
    if (!hotCache || !hotCache.hidden_total) { toast('没有被移除的内置热词', 'bad'); return; }
    if (!confirm(`恢复 ${hotCache.hidden_total} 个被移除的内置热词？`)) return;
    try {
      await postJSON('/api/builtin/restore', { kind: 'hotwords' });
      toast('已恢复全部内置热词', 'ok');
      loadHotwords();
    } catch (e) { toast(e.message, 'bad'); }
  });

  $('#btnHotImport').addEventListener('click', () => $('#hotFile').click());
  $('#hotFile').addEventListener('change', async e => {
    const f = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!f) return;
    const note = $('#hotImportNote');
    note.textContent = '正在解析 ' + f.name + ' …';
    const fd = new FormData(); fd.append('file', f, f.name);
    try {
      const r = await api('/api/hotwords/import', { method: 'POST', body: fd });
      const parts = [`识别为${r.format}`, `新增 ${r.added} 个词`];
      if (r.skipped) parts.push(`跳过 ${r.skipped} 个（重复）`);
      if (r.terms && r.terms.length) parts.push('如：' + r.terms.slice(0, 4).join('、'));
      note.textContent = parts.join(' · ');
      toast(`已从 ${f.name} 导入 ${r.added} 个热词` + (r.skipped ? `，跳过 ${r.skipped} 个重复` : ''), r.added ? 'ok' : 'bad');
      loadHotwords();
    } catch (err) {
      note.textContent = '导入失败：' + err.message;
      toast('导入失败：' + err.message, 'bad');
    }
  });

  $('#btnHotAddDo').addEventListener('click', async () => {
    const raw = $('#hotNew').value.trim();
    if (!raw) { toast('请先输入热词', 'bad'); return; }
    try {
      const r = await postJSON('/api/hotwords', { terms: raw, group: $('#hotGroup').value });
      if (r.added) {
        toast(`已添加 ${r.added} 个热词` + (r.skipped ? `，跳过 ${r.skipped} 个重复` : ''), 'ok');
        $('#hotNew').value = '';
        loadHotwords();
      } else {
        toast('这些词都已存在，无需重复添加', 'bad');
      }
    } catch (e) { toast(e.message, 'bad'); }
  });

  $('#btnHotClear').addEventListener('click', async () => {
    if (!confirm('清空你导入和手动添加的全部热词？\n\n内置热词库不受影响。')) return;
    try {
      const r = await postJSON('/api/hotwords/clear-user');
      toast(`已清空 ${r.removed} 个个人热词`, 'ok');
      $('#hotImportNote').textContent = '';
      loadHotwords();
    } catch (e) { toast(e.message, 'bad'); }
  });

  $('#btnHotTpl').addEventListener('click', () => downloadTemplate('hotwords'));

  async function downloadTemplate(kind) {
    try {
      const r = await api(`/api/template/${kind}`);
      // 带 BOM，双击用记事本打开不会乱码
      const blob = new Blob(['\uFEFF' + r.content], { type: 'text/plain;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = r.filename;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
      toast('模板已下载，可参照格式填写后导入');
    } catch (e) { toast(e.message, 'bad'); }
  }

  /* ═════════════ 系统状态：磁盘占用（侧边栏常驻） ═════════════ */

  // ★ 安全绑定的意义：这个项目里整块卡片会被搬来搬去（比如把「磁盘占用」
  //   从设置页挪到侧边栏）。直接用 $('#x').addEventListener 一旦元素被挪走/改名
  //   就会抛 TypeError，**整个 app.js 初始化中断，界面全白**——
  //   而报错信息只会指向这一行，看不出是"控件没了"。
  //   所有非关键绑定都走这里，元素不在就静默跳过。
  function safeOn(sel, evt, fn) {
    const el = $(sel);
    if (!el) { console.warn('[ui] 绑定跳过，元素不存在:', sel); return null; }
    el.addEventListener(evt, fn);
    return el;
  }

  const DISK_SEGS = [
    { key: 'models_mb',  label: '模型',        color: 'var(--accent)' },
    { key: 'uploads_mb', label: '上传素材',    color: 'var(--accent-2)' },
    { key: null,         label: '临时缓存',    color: 'var(--warn)',
      pick: s => (s.cache && s.cache.size_mb) || 0 },
    { key: 'exports_mb', label: '导出结果',    color: 'var(--text-3)' },
  ];

  async function loadStorage() {
    try {
      const s = await api('/api/storage');
      const mb = v => fmtSize((v || 0) * 1024 * 1024);
      $('#storageHint').textContent = `磁盘剩余 ${s.disk_free_gb} GB`;

      const rows = DISK_SEGS.map(x => ({
        label: x.label, color: x.color,
        v: x.pick ? x.pick(s) : (s[x.key] || 0),
      }));
      const total = rows.reduce((a, r) => a + Math.max(0, r.v), 0);

      // 一条堆叠条：比四个数字更快看出"谁在吃磁盘"
      const stack = $('#storageStack');
      if (stack) {
        stack.innerHTML = total > 0
          ? rows.filter(r => r.v > 0).map(r =>
              `<i style="width:${(r.v / total * 100).toFixed(2)}%;background:${r.color}" ` +
              `title="${r.label} ${mb(r.v)}"></i>`).join('')
          : '<i style="width:100%;background:var(--surface-2)"></i>';
      }

      const leg = $('#storageLegend');
      if (leg) {
        leg.innerHTML = rows.map(r =>
          `<li><i style="background:${r.color}"></i><span class="n">${r.label}</span>` +
          `<span class="v">${mb(r.v)}</span></li>`).join('');
      }

      const c = $('#cacheNote');
      if (c) {
        c.textContent = s.cache && s.cache.files
          ? `临时缓存 ${s.cache.files} 个文件（任务结束会自动删）`
          : '暂无临时缓存';
      }
      state.storage = s;
    } catch (e) {
      const h = $('#storageHint');
      if (h) h.textContent = '磁盘信息读取失败';
    }
  }


  /* ═════════════ 释放空间（逐项勾选 + 二次确认） ═════════════ */
  const KIND_META = {
    safe:    { pill: '可放心删', cls: '' },
    caution: { pill: '删后需重新下载', cls: 'warn' },
    danger:  { pill: '删了就没了', cls: 'danger' },
  };

  async function openCleanModal() {
    await loadStorage();
    const s = state.storage;
    if (!s) { toast('读取磁盘占用失败', 'bad'); return; }
    if (s.busy) {
      toast('有任务正在执行，先等它跑完或取消后再清理', 'bad');
      return;
    }
    const total = (s.targets || []).reduce((a, t) => a + (t.size_mb || 0), 0);
    $('#cleanLead').textContent =
      `磁盘当前剩余 ${s.disk_free_gb} GB。下面列出程序占用空间的各项，勾选要清理的内容：`;
    $('#cleanList').innerHTML = (s.targets || []).map(t => {
      const meta = KIND_META[t.kind] || KIND_META.safe;
      const size = t.size_mb > 0 ? fmtSize(t.size_mb * 1024 * 1024) : '0 B';
      const empty = t.size_mb <= 0;
      return `<label class="cleanrow${empty ? ' zero' : ''}" data-id="${t.id}">
        <input type="checkbox" data-clean="${t.id}" ${t.default_on && !empty ? 'checked' : ''} ${empty ? 'disabled' : ''}>
        <span class="cz">
          <span class="czl">${esc(t.label)}</span>
          <span class="czs">${esc(t.detail)}</span>
        </span>
        <span class="cv">${esc(size)}</span>
        <span class="mini-pill ${meta.cls}">${meta.pill}</span>
      </label>`;
    }).join('') + `<p class="note" style="margin-top:10px">合计可释放约 ${fmtSize(total * 1024 * 1024)}。</p>`;

    $$('#cleanList [data-clean]').forEach(cb => cb.addEventListener('change', refreshCleanState));
    refreshCleanState();
    $('#cleanModal').classList.add('on');
  }

  function selectedCleanTargets() {
    return $$('#cleanList [data-clean]').filter(cb => cb.checked).map(cb => cb.dataset.clean);
  }

  function refreshCleanState() {
    const picked = selectedCleanTargets();
    const s = state.storage || { targets: [] };
    let mb = 0;
    picked.forEach(id => {
      const t = (s.targets || []).find(x => x.id === id);
      if (t) mb += t.size_mb || 0;
    });
    const hasDanger = picked.includes('exports');
    $('#cleanStatus').textContent = picked.length
      ? `已选 ${picked.length} 项，约 ${fmtSize(mb * 1024 * 1024)}`
      : '未选择任何内容';
    $('#cleanStatus').className = 'modal-status' + (hasDanger ? ' bad' : '');
    const go = $('#cleanGo');
    go.disabled = picked.length === 0;
    go.textContent = hasDanger ? '确认删除（含成果文件）' : '释放选中项';
  }

  function closeCleanModal() { $('#cleanModal').classList.remove('on'); }
  $('#btnCleanSpace').addEventListener('click', openCleanModal);
  $('#cleanClose').addEventListener('click', closeCleanModal);
  $('#cleanCancel').addEventListener('click', closeCleanModal);

  $('#cleanGo').addEventListener('click', async () => {
    const picked = selectedCleanTargets();
    if (!picked.length) return;
    const s = state.storage || { targets: [] };
    const rows = picked.map(id => {
      const t = (s.targets || []).find(x => x.id === id) || { label: id, size_mb: 0 };
      return `· ${t.label}（${fmtSize((t.size_mb || 0) * 1024 * 1024)}）`;
    }).join('\n');

    // ★ 二次确认：写清"删什么、释放多少、后果是什么"
    const hasDanger = picked.includes('exports');
    const hasModels = picked.includes('models');
    let warn = '';
    if (hasDanger) warn += '\n\n⚠️ 包含「导出的转写结果」—— 那是你的成果文件，删掉后程序里无法恢复。';
    if (hasModels) warn += '\n\n注意：模型删掉后，下次转写需要重新下载。';
    if (!confirm(`将释放以下内容：\n\n${rows}${warn}\n\n确定继续吗？`)) return;

    // 涉及成果文件时再要一次，避免手滑
    if (hasDanger && !confirm('最后确认一次：真的要删除导出的转写结果吗？\n\n这个操作不可撤销。')) return;

    const btn = $('#cleanGo');
    btn.disabled = true; btn.textContent = '正在释放…';
    try {
      const r = await postJSON('/api/storage/cleanup', { targets: picked });
      const failed = (r.results || []).filter(x => !x.ok);
      const okList = (r.results || []).filter(x => x.ok);
      toast(okList.length
        ? `已释放 ${r.freed_gb} GB（${okList.map(x => x.label).join('、')}）`
        : '没有释放任何空间', okList.length ? 'ok' : 'bad');
      if (failed.length) {
        toast(`有 ${failed.length} 项没成功：${failed[0].label} —— ${failed[0].message}`, 'bad');
      }
      closeCleanModal();
      await loadStorage();
      await loadEnv();
      if (s.busy !== undefined) toast('磁盘剩余 ' + r.disk_free_after_gb + ' GB');
    } catch (e) {
      toast(e.message, 'bad');
      btn.disabled = false;
      refreshCleanState();
    }
  });

  /* ═════════════ 环境体检（逐项检查 + 一键修复） ═════════════ */
  let healthData = null;
  let fixTimer = null;

  async function loadHealth(refresh) {
    const v = $('#healthVerdict');
    v.textContent = refresh ? '正在重新检查…' : '正在检查…';
    v.className = 'verdict';
    try {
      healthData = await api('/api/health-check' + (refresh ? '?refresh=1' : ''));
      renderHealth();
      renderDiag();
    } catch (e) {
      v.textContent = '检查失败：' + e.message;
      v.className = 'verdict bad';
    }
  }

  function hrow(i) {
    const mk = { ok: '✓', warn: '!', fail: '✕', optional: '—' }[i.status] || '?';
    const isOpt = i.status === 'optional';
    const badges = [];
    // 只在「有状态异常」时才标「可选」——正常项上这个标签纯属噪音
    if (!i.required && !isOpt && i.status !== 'ok') badges.push('<span class="mini-pill">可选</span>');
    if (isOpt) badges.push('<span class="mini-pill">按需安装</span>');
    if (!isOpt && i.status !== 'ok' && i.fixable) {
      badges.push('<span class="mini-pill" style="color:var(--accent)">可自动修复</span>');
    }
    // 未安装的模型不是问题，所以不显示「怎么办」那行提示（详情里已经写了体积和用途）
    const hint = !isOpt && i.status !== 'ok' && i.fix_hint
      ? `<p class="hint2">${esc(i.fix_hint)}</p>` : '';
    const btn = i.fixable
      ? `<div class="act"><button class="btn sm${isOpt ? ' ghost' : ''}" data-fix="${esc(i.id)}">${isOpt ? '下载' : '修复'}</button></div>`
      : '';
    return `<div class="hrow ${i.status}">
      <span class="mk">${mk}</span>
      <div class="body">
        <p class="lb">${esc(i.label)}${badges.join('')}</p>
        <p class="dt">${esc(i.detail)}</p>
        ${hint}
      </div>
      ${btn}
    </div>`;
  }

  function renderHealth() {
    const d = healthData;
    if (!d) return;
    const v = $('#healthVerdict');
    v.textContent = d.verdict;
    // 未安装的模型只是「还没下」，不算问题 —— 它单独占一格统计，
    // 既不计入「需要留意」，也不让结论横幅变黄。
    v.className = 'verdict ' + (d.blocking ? 'bad' : (d.problem ? 'warn' : 'ok'));
    $('#healthAt').textContent = `检查时间 ${d.checked_at} · 共 ${d.total} 项`;

    $('#healthStats').innerHTML = [
      ['检查项', d.total + ' 项', ''],
      ['正常', d.ok + ' 项', 'accent'],
      ['待装模型', (d.optional || 0) + ' 个', ''],
      ['问题项', (d.problem || 0) + ' 项', d.problem ? 'warn' : 'accent'],
    ].map(([k, val, c]) => `<div class="metric"><p class="k">${k}</p><p class="v ${c}">${esc(val)}</p></div>`).join('');

    const byGroup = {};
    d.items.forEach(i => { (byGroup[i.group] = byGroup[i.group] || []).push(i); });
    $('#healthList').innerHTML = (d.groups || []).map(g => {
      const rows = (byGroup[g] || []).map(i => hrow(i)).join('');
      return `<div class="ghead">${esc(g)}</div><div class="rulebox">${rows}</div>`;
    }).join('');

    $$('#healthList [data-fix]').forEach(b =>
      b.addEventListener('click', () => startFix([b.dataset.fix])));

    const btn = $('#btnHealthFix');
    btn.disabled = !d.fixable;
    btn.textContent = d.fixable ? `诊断修复（${d.fixable} 项）` : '诊断修复';
    // 没有待修问题时，把「可安装模型」的入口指回模型中心，避免用户以为没救了
    const note = $('#healthFixNote');
    if (note) {
      note.textContent = d.fixable
        ? '只处理上面标出的问题项，不会动其他东西'
        : (d.installable
          ? `当前没有需要修复的问题。${d.installable} 个未安装的模型可按需下载（点各行「下载」，或去模型中心）。`
          : '当前没有需要修复的问题。');
      note.className = 'note' + (d.fixable ? '' : ' ok');
    }
  }

  async function startFix(ids) {
    try {
      const r = await postJSON('/api/health-fix', ids ? { ids } : {});
      $('#fixCard').style.display = '';
      toast(`开始修复 ${r.count} 项，过程中可以继续用其他功能`, 'ok');
      pollFix();
    } catch (e) { toast(e.message, 'bad'); }
  }

  function pollFix() {
    if (fixTimer) return;
    pollFixOnce();
    fixTimer = setInterval(pollFixOnce, 1200);
  }

  async function pollFixOnce() {
    let s;
    try { s = await api('/api/health-fix/status'); } catch (_) { return; }
    renderFix(s);
    if (!s.running && s.total) {
      if (fixTimer) { clearInterval(fixTimer); fixTimer = null; }
      const ok = s.steps.filter(x => x.status === 'ok').length;
      const bad = s.steps.filter(x => x.status === 'fail').length;
      await loadHealth(true);
      toast(bad ? `修复结束：${ok} 项成功、${bad} 项失败` : `修复完成：${ok} 项已修好`, bad ? 'bad' : 'ok');
    }
  }

  function renderFix(s) {
    const pct = s.total ? (s.done / s.total * 100) : 0;
    $('#fixBar').style.width = pct + '%';
    $('#fixHint').textContent = s.running
      ? `进行中 ${s.done}/${s.total}` : `已结束 ${s.done}/${s.total}`;
    $('#btnFixStop').disabled = !s.running;
    $('#fixSteps').innerHTML = (s.steps || []).map(st => `
      <div class="fixstep ${st.status}">
        <span class="dot2"></span>
        <span class="nm2">${esc(st.label)}</span>
        <span class="ms">${esc(st.message || '等待…')}</span>
      </div>`).join('');
    $('#fixNote').textContent = s.running ? `已用 ${fmtDur(s.elapsed)}` : '';
  }

  $('#btnHealthRefresh').addEventListener('click', async () => {
    toast('正在重新检查…');
    await loadHealth(true);
    toast('检查完成', 'ok');
  });

  $('#btnHealthFix').addEventListener('click', () => {
    if (!healthData || !healthData.fixable) return;
    const n = healthData.fixable;
    if (!confirm(`将修复 ${n} 个可自动处理的项目：\n\n`
      + `· 缺少的依赖会用国内镜像自动安装（可能几分钟）\n`
      + `· 未安装或损坏的模型会重新下载\n`
      + `· 不可写的目录会尝试重建\n\n`
      + `过程中可以继续用其他功能，修完会自动重查。`)) return;
    startFix(null);
  });

  $('#btnFixStop').addEventListener('click', async () => {
    try { await postJSON('/api/health-fix/stop'); toast('已请求停止'); }
    catch (e) { toast(e.message, 'bad'); }
  });

  /* ═════════════ 原始报告 ═════════════ */
  function renderDiag() {
    $('#diagBox').textContent = JSON.stringify(
      { env: state.env, health: healthData }, null, 2);
  }

  $('#btnRecheck').addEventListener('click', async () => {
    toast('正在重新检测…');
    await loadEnv();
    toast('检测完成，可用性判定已刷新', 'ok');
  });

  $('#btnCopyDiag').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('#diagBox').textContent);
      toast('诊断报告已复制', 'ok');
    } catch (_) { toast('复制失败，请手动选择文本', 'bad'); }
  });


  $('#btnExportOpen').addEventListener('click', () => {
    const j = state.jobs.find(x => x.exports && Object.keys(x.exports).length);
    const dir = j ? Object.values(j.exports)[0].replace(/[\\/][^\\/]+$/, '') : '';
    if (!dir) { toast('还没有导出过文件', 'bad'); return; }
    openFolder(dir);
  });

  /* ═════════════ 初始化 ═════════════ */
  (async function init() {
    let saved = 'space';
    try { saved = localStorage.getItem('sb.theme') || 'space'; } catch (_) {}
    applyTheme(saved);

    // 支持 #models 这样的直达链接（刷新后停在原页面）
    const hash = (location.hash || '').replace('#', '');
    if (VIEWS.includes(hash)) go(hash, true);

    await loadEnv();
    // ★ 能力矩阵尽早加载 —— 工作台的 VAD 说明、引擎下拉里的"不生效"提示都靠它。
    //   必须在 restoreStaging / renderStaging 之前，否则第一次画出的下拉没有标注。
    await loadCapabilities();
    // ★ 系统状态现在常驻侧边栏，必须启动时就加载 ——
    //   否则任何页面都显示"磁盘读取中…"，直到用户切到设置页才填上。
    await loadStorage();
    await loadExportDir();          // 工作台的「导出到」
    await refreshJobs();
    restoreStaging();          // 刷新后还原待确认状态
    await loadCorrections();
    await loadHotwords();

    if (state.env) {
      $('#exportPathNote').textContent = '导出根目录：data/exports（默认按日期分文件夹）';
    }

    // 有下载在跑（例如刷新页面后），接着轮询
    try {
      const d = await api('/api/models/downloads');
      if ((d.downloads || []).some(x => x.status === 'running')) {
        (d.downloads || []).forEach(x => { state.downloads[x.model_id] = x; });
        startDownloadPolling();
      }
    } catch (_) {}

    const active = state.jobs.find(j => ['running', 'paused'].includes(j.status));
    if (active) {
      startJobPolling();
      await followJob(active.id);
    } else if (state.jobs.some(j => ['running', 'paused', 'queued'].includes(j.status))) {
      startJobPolling();
    }
  })();
})();
