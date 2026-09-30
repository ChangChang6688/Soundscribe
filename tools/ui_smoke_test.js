/* 声文 · 界面冒烟测试
 *
 * 目的：**用真实浏览器把每个交互点一遍**，而不是只测接口通不通。
 * 起因：Eli 反馈「选了导出文件夹但没切换过去，按钮是失效的」——
 *      接口层面查不出这类问题，必须真的点。
 *
 * 用法（Windows / git bash）：
 *   node tools/ui_smoke_test.js [--headful]
 *
 * 依赖 playwright-core（复用系统已装的 Chrome，不下载 Chromium）：
 *   npm install playwright-core
 */
const PW = process.env.PW_PATH || 'C:/Users/Eli/node_modules/playwright-core';
const { chromium } = require(PW);

const BASE = process.env.SB_BASE || 'http://127.0.0.1:8765';
const HEADFUL = process.argv.includes('--headful');
const FILE = process.env.SB_FILE || 'data/en-audio-16k.wav';

let pass = 0, fail = 0;
const failures = [];

/* ★ 测试必须自己清理现场。
   否则上一轮遗留的任务会一直占着 GPU/CPU，让后续请求变慢，
   下一轮就会出现一堆"莫名其妙"的失败 —— 实测踩过，被误判成应用坏了。 */
/* ★★ 测试绝不能碰用户已有的数据。
 *
 * 这里踩过一次真实的坑：原来的实现是"进入时把任务列表清空、结束时再清空"，
 * 结果把用户自己转写过的任务记录一起删了 —— 导出文件还在磁盘上，
 * 但任务列表、段落、统计全没了。用户不可能预期"跑个自检把我的手稿清了"。
 *
 * 正确做法：**开工先拍快照，之后只删本次新建的**。
 * 快照之外的一律不碰，哪怕它看起来是"遗留垃圾"。
 */
let PRE_EXISTING_JOBS = new Set();
let SNAPSHOT_TAKEN = false;

async function snapshotJobs() {
  try {
    const r = await fetch(BASE + '/api/jobs').then(x => x.json());
    PRE_EXISTING_JOBS = new Set((r.jobs || []).map(j => j.id));
    SNAPSHOT_TAKEN = true;
    if (PRE_EXISTING_JOBS.size) {
      console.log('  （开工快照：已有 ' + PRE_EXISTING_JOBS.size + ' 条任务，本次不会删除它们）');
    }
  } catch (_) {
    // 快照失败时宁可"什么都不删"，也不要冒险删用户的
    PRE_EXISTING_JOBS = new Set();
    SNAPSHOT_TAKEN = false;
    console.log('  （⚠ 任务快照失败：本次将不清理任何任务，以免误删用户数据）');
  }
}

async function cleanupJobs(label) {
  if (!SNAPSHOT_TAKEN) return;      // 没取到快照 → 一律不动
  try {
    const r = await fetch(BASE + '/api/jobs').then(x => x.json());
    const mine = (r.jobs || []).filter(j => !PRE_EXISTING_JOBS.has(j.id));
    for (const j of mine) {
      if (j.status === 'running' || j.status === 'paused') {
        await fetch(BASE + '/api/jobs/' + j.id + '/cancel', { method: 'POST' }).catch(() => {});
        await new Promise(res => setTimeout(res, 1200));
      }
      await fetch(BASE + '/api/jobs/' + j.id, { method: 'DELETE' }).catch(() => {});
    }
    if (mine.length) {
      console.log('  （' + label + '：清理本次新建的 ' + mine.length + ' 条任务；'
        + '开工前已有的 ' + PRE_EXISTING_JOBS.size + ' 条原样保留）');
    }
  } catch (_) { /* 清理失败不阻断测试 */ }
}

function ok(name, cond, extra) {
  if (cond) { pass++; console.log('  \u2713 ' + name + (extra ? '  — ' + extra : '')); }
  else { fail++; failures.push(name); console.log('  \u2717 ' + name + (extra ? '  — ' + extra : '')); }
}

(async () => {
  await snapshotJobs();   // ★ 只拍快照，不动任何已有任务
  const browser = await chromium.launch({ channel: 'chrome', headless: !HEADFUL });
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const page = await ctx.newPage();

  /* ── 收集所有前端错误：JS 异常、控制台报错、请求失败 ── */
  const jsErrors = [], netErrors = [];
  page.on('pageerror', e => jsErrors.push(e.message));
  page.on('console', m => {
    if (m.type() !== 'error') return;
    const t = m.text();
    const loc = (m.location && m.location()) || {};
    const url = (loc.url || '').replace(BASE, '');
    if (/favicon/i.test(t) || /favicon/i.test(url)) return;
    jsErrors.push(t + (url ? '  @ ' + url : ''));
  });
  page.on('requestfailed', r => {
    const u = r.url();
    if (!/favicon/i.test(u)) netErrors.push(u.replace(BASE, '') + ' :: ' + ((r.failure() || {}).errorText || ''));
  });
  page.on('response', r => {
    if (r.status() >= 400 && r.url().startsWith(BASE) && !/favicon/i.test(r.url())) {
      netErrors.push(r.url().replace(BASE, '') + ' → HTTP ' + r.status());
    }
  });

  try {
    /* ═══ 1. 加载与基础渲染 ═══ */
    /* ═══ 0. ★ 前端资源必须能更新（缓存回归） ═══ */
  // ★ 这一条是被真实问题逼出来的：静态资源没有显式缓存策略时，
  //   浏览器走启发式缓存，会出现"后端改了、也重启了、接口返回的确实是新文件，
  //   但浏览器连请求都不发"——界面看起来没更新。
  //   所以：入口页必须给资源 URL 带上内容指纹，并且两级都声明缓存策略。
  console.log('\n【0】前端资源可更新性（缓存回归）');
  {
    // 本段位于主导航之前，先确保页面已在应用上（否则 fetch('/') 没有 base URL 可用）
    if (!page.url().startsWith(BASE)) {
      await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(1800);
    }
    const html = await page.evaluate(async () =>
      await (await fetch('/', { cache: 'no-store' })).text());
    ok('★ 入口页给 JS/CSS 带了内容指纹', /app\.js\?v=[0-9a-f]{6,}/.test(html) &&
      /app\.css\?v=[0-9a-f]{6,}/.test(html),
      (html.match(/app\.js\?v=[0-9a-f]+/) || ['（未找到）'])[0]);
    const build = await page.evaluate(() => window.__BUILD__ || '');
    ok('★ 页面暴露构建指纹（便于排查"看到的是不是最新"）', /^[0-9a-f]{6,}$/.test(build), build);
    const entries = await page.evaluate(() => performance
      .getEntriesByType('resource')
      .filter(e => /app\.(js|css)/.test(e.name))
      .map(e => e.name.split('/static/')[1]));
    ok('★ 实际加载的资源 URL 带指纹（未被旧缓存顶掉）',
      entries.every(n => /\?v=[0-9a-f]{6,}/.test(n)), entries.join(' / '));
  }

  console.log('\n【1】页面加载与基础渲染');
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(3000);

    ok('页面标题正确', (await page.title()).includes('声文'));
    ok('环境自检完成（档位已显示）',
      !(await page.textContent('#envPill')).includes('正在自检'),
      (await page.textContent('#envPill')).trim());
    ok('引擎状态已显示', !(await page.textContent('#enginePill')).includes('检测中'),
      (await page.textContent('#enginePill')).trim());
    // ★★ 推理设备必须显示出来 —— 这是 Eli 报"是不是全跑在 CPU 上"的直接产物。
    //    以前界面上完全没有设备信息，用户只能靠猜；而 provider 是会静默回落的，
    //    所以"实际用的是 CPU 还是 GPU"必须由后端探测后如实报出来。
    const devTxt = (await page.textContent('#devicePill') || '').trim();
    ok('★ 侧边栏显示推理设备（CPU / GPU）',
      /推理：/.test(devTxt) && !/检测中/.test(devTxt), devTxt);
    ok('★ 设备胶囊带说明（悬停能看到为什么）',
      ((await page.getAttribute('#devicePill', 'title')) || '').length > 4,
      ((await page.getAttribute('#devicePill', 'title')) || '').slice(0, 60));
    ok('侧栏 5 个导航项',
      (await page.$$eval('#nav button', els => els.length)) === 5);

    /* ═══ 2. 导出位置卡片（本轮重点） ═══ */
    console.log('\n【2】导出位置');
    ok('导出输入框存在', await page.$('#exportDir') !== null);
    const note0 = (await page.textContent('#exportNote')).trim();
    ok('导出提示文字已渲染', note0.length > 0, note0.slice(0, 60));
    const quickBtns = await page.$$eval('#quickDirs .chip-btn', els => els.map(e => e.textContent.trim()));
    ok('快捷位置按钮已生成', quickBtns.length >= 4, quickBtns.join(' / '));
    ok('「与源文件同目录」在快捷项里', quickBtns.some(t => t.includes('同目录')));

    // 点「选择文件夹…」→ 应用内弹层
    await page.click('#btnPickDir');
    await page.waitForTimeout(2500);
    ok('目录弹层已打开', await page.isVisible('#dirModal .modal-box'));
    const roots = await page.$$eval('#dirRoots .dir-item', els => els.map(e => e.textContent.trim()));
    ok('弹层左侧列出常用位置与磁盘', roots.length >= 3, roots.slice(0, 5).join(' / '));
    const rows = await page.$$eval('#dirList .dir-row', els => els.length);
    ok('弹层右侧列出子文件夹', rows > 0, rows + ' 个');

    // 进入第一个子文件夹，再「使用这个文件夹」
    if (rows > 0) {
      await page.click('#dirList .dir-row');
      await page.waitForTimeout(2000);
    }
    const picked = await page.inputValue('#dirPathInput');
    await page.click('#dirUse');
    await page.waitForTimeout(1500);
    const applied = await page.inputValue('#exportDir');
    ok('★ 选择后导出目录真的切换了', !!applied && applied === picked,
      '选中「' + picked + '」→ 输入框「' + applied + '」');
    ok('弹层已关闭', !(await page.isVisible('#dirModal .modal-box')));
    await page.waitForTimeout(800);
    const note1 = (await page.textContent('#exportNote')).trim();
    ok('底部提示已更新为该目录', note1.includes(applied) || note1.includes('剩余'), note1.slice(0, 70));

    // 恢复默认，避免影响后续
    await page.click('#btnResetDir');
    await page.waitForTimeout(700);

    /* ═══ 3. 拖入文件 → 待开始列表 ═══ */
    console.log('\n【3】多文件拖入与待开始列表');
    const abs = require('path').resolve(FILE);
    // #filepick 平时是 display:none，Playwright 要求元素可见才能设置文件。
    // 临时显示一下再恢复 —— 验证的是 handleFiles 逻辑，不是这个 input 的可见性。
    await page.evaluate(() => { document.getElementById('filepick').style.display = 'block'; });
    await page.setInputFiles('#filepick', [abs]);
    await page.evaluate(() => { document.getElementById('filepick').style.display = 'none'; });
    await page.waitForTimeout(3500);
    ok('待开始面板出现', await page.isVisible('#stageCard'));
    const srows = await page.$$eval('#stageList .srow', els => els.length);
    ok('待开始列表有 1 项', srows === 1, srows + ' 项');
    ok('该项显示了时长标签',
      (await page.textContent('#stageList .srow .sname')).match(/\d{2}:\d{2}/) !== null,
      (await page.textContent('#stageList .srow .sname')).trim().slice(0, 40));
    ok('★ 每项都有独立的「开始」按钮',
      await page.$('#stageList .srow button[data-act="start"]') !== null);
    ok('引擎下拉可选', await page.$('#stageList .srow .msel[data-act="engine"]') !== null);
    ok('底部的批量开始按钮也在', await page.$('#btnStartAll') !== null);

    /* ═══ 4. 点单项「开始」 ═══ */
    console.log('\n【4】单个开始 → 暂停 → 继续');
    await page.click('#stageList .srow button[data-act="start"]');
    await page.waitForTimeout(4000);
    ok('★ 点单项开始后，该项从待开始列表移除',
      (await page.$$eval('#stageList .srow', els => els.length)) === 0);
    ok('任务卡片出现', await page.isVisible('#jobCard'));
    const jobName = (await page.textContent('#jobName')).trim();
    ok('任务名正确', jobName.length > 1 && jobName !== '—', jobName);

    // ★ 暂停要趁早：GPU 上 3 分钟音频几秒就跑完了，
    //   等太久会撞上"任务已完成"，那时按钮已禁用，测不到暂停。
    //   这里点完开始后立刻尝试，不再额外等待。
    if (await page.$('#btnPause:not([disabled])')) {
      await page.click('#btnPause');
      // ① 点下去要立刻有反应（不能等后端事件，否则像是按钮坏了）
      await page.waitForTimeout(500);
      const immediate = (await page.textContent('#btnPause')).trim();
      ok('★ 点暂停后界面立刻有反馈',
        immediate === '正在暂停…' || immediate === '继续', '按钮显示「' + immediate + '」');

      // ② 真正生效。判断依据用**按钮文本**而不是状态栏文字 ——
      //    状态栏里那句"已请求暂停…"是乐观提示，本身也含"暂停"二字，
      //    拿它判断会把「已请求」误当成「已生效」。
      //    抽音轨等阶段无法中断，最多等 15 秒。
      let paused = false;
      for (let i = 0; i < 30; i++) {
        await page.waitForTimeout(500);
        const label = (await page.textContent('#btnPause')).trim();
        if (label === '继续') { paused = true; break; }
        const t = await page.textContent('#jobStats');
        if (t.includes('已完成') || t.includes('失败')) break;
      }
      ok('暂停最终生效（按钮切换到「继续」）', paused,
        (await page.textContent('#jobStats')).trim().slice(0, 55));

      if (paused) {
        const segBefore = (await page.textContent('#txCount')).trim();
        await page.waitForTimeout(3000);
        const segAfter = (await page.textContent('#txCount')).trim();
        ok('暂停期间不再产出新内容', segBefore === segAfter, segBefore + ' 段 → ' + segAfter + ' 段');

        await page.click('#btnPause');
        let resumed = false;
        for (let i = 0; i < 20; i++) {
          await page.waitForTimeout(500);
          if ((await page.textContent('#btnPause')).trim() !== '继续') { resumed = true; break; }
        }
        ok('继续生效', resumed, (await page.textContent('#jobStats')).trim().slice(0, 55));
      }
    } else {
      ok('暂停按钮可点', false, '按钮不可用（任务可能已经跑完）');
    }

    /* ═══ 5. 设置页：词库管理 ═══ */
    /* ═══ 4.4 引擎下拉（自绘组件） ═══ */
    console.log('\n【4.4】引擎下拉（自绘，非原生 select）');
    {
      await page.setInputFiles('#filepick',
        'C:/Users/Eli/WorkBuddy/workbuddy/soundscribe/m1/asr_example_zh.wav');
      await page.waitForTimeout(3000);
      const hasSel = await page.$('#stageList .msel[data-act="engine"]') !== null;
      ok('用自绘下拉（不是原生 select）', hasSel &&
        (await page.$('#stageList select[data-act="engine"]')) === null);
      const trigger = await page.$eval('#stageList .msel[data-act="engine"]',
        el => ({ v: el.dataset.value,
                 text: el.querySelector('.mv').textContent.replace(/\s+/g, ' ').trim() }));
      ok('★ 默认是「自动判定」并带标注', trigger.v === 'auto' && /自动判定（.+）/.test(trigger.text),
        trigger.text);
      // 打开浮层，检查选项带"适合什么"的标注
      await page.click('#stageList .msel[data-act="engine"]');
      await page.waitForTimeout(450);
      ok('点开后有浮层', await page.isVisible('#stageList .msel.open .mmenu'));
      const opts = await page.$$eval('#stageList .msel.open .mmenu .mo',
        els => els.map(e => e.textContent.replace(/\s+/g, ' ').trim()));
      ok('★ 选项数 = 全部可用引擎（不是硬编码 3 项）', opts.length >= 5, opts.length + ' 项');
      ok('★ 每个选项都标注了"适合什么"', opts.slice(1).every(t => /（.+）/.test(t)),
        opts.join(' | ').slice(0, 90));

      // ★★ 每一项都必须"真的能被点到" —— 这是 2026-09-30 Eli 报的那个 bug 的回归断言。
      //
      //   现象：下拉展开后，下半截被下面的「转写选项」卡片盖住，
      //         靠后的模型（Moonshine / Whisper）**根本点不到**。
      //   机理：`.card` 上的 `backdrop-filter` 创建了层叠上下文，
      //         浮层的 z-index 突破不出去和兄弟卡片比。
      //
      //   ★ 为什么之前的测试没抓到：它只检查了"DOM 里有 5 项"，
      //     然后点**第一项**（能点到）就通过了。被盖住的恰好是后面几项。
      //     **elementFromPoint 是唯一能揭穿"元素在 DOM 里、但被别的元素盖住"的办法** ——
      //     getBoundingClientRect 对此毫无反应，元素照样报自己有宽高。
      const occl = await page.evaluate(() => {
        const menu = document.querySelector('#stageList .msel.open .mmenu');
        if (!menu) return { err: '菜单没打开' };
        const mr = menu.getBoundingClientRect();
        const blocked = [];
        const items = menu.querySelectorAll('.mo');
        items.forEach(btn => {
          const r = btn.getBoundingClientRect();
          const x = r.left + r.width / 2;
          const y = r.top + r.height / 2;
          // 滚出菜单可视区的项跳过（那种情况用户滚动就能点到，不算 bug）
          if (y < mr.top + 1 || y > mr.bottom - 1) return;
          const hit = document.elementFromPoint(x, y);
          if (!hit || (hit !== btn && !btn.contains(hit))) {
            const name = (btn.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 14);
            blocked.push(name + ' ←被 ' +
              (hit ? (hit.className || hit.tagName).toString().slice(0, 22) : '视口外') + ' 挡住');
          }
        });
        return { blocked, total: items.length, menuId: mr.top.toFixed(0) };
      });
      ok('★★ 下拉里每一项都没被遮挡（真的能点到）',
        !occl.err && (occl.blocked || []).length === 0,
        occl.err || (`${occl.total} 项，被挡 ${occl.blocked.length} 项：` +
                     (occl.blocked.slice(0, 3).join(' ／ ') || '无')));

      // ★ 最后一项单独验一次：被遮挡通常从下往上发生，最后一项最可能中招
      const lastOk = await page.evaluate(async () => {
        const items = [...document.querySelectorAll('#stageList .msel.open .mmenu .mo')];
        const last = items[items.length - 1];
        if (!last) return { ok: false, why: '没有选项' };
        const r = last.getBoundingClientRect();
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return { ok: hit === last || last.contains(hit),
                 why: last.textContent.replace(/\s+/g, ' ').trim().slice(0, 16) };
      });
      ok('★★ 最后一项（最容易被盖住的那项）可点', lastOk.ok, lastOk.why);

      // ★ 修复机制本身要在场：展开时承载卡片的 class 必须被加上
      ok('★ 展开时卡片被提升（否则会被后面的卡片盖住）',
        await page.evaluate(() => {
          const open = document.querySelector('#stageList .msel.open');
          const card = open && open.closest('.card');
          return !!card && card.classList.contains('lift');
        }));

      // ★★ 反向验证：临时撤销"卡片提升"，确认上面那条断言**真的能检出遮挡**。
      //    不做这一步的话，"断言通过"可能只是因为断言写错了 ——
      //    这个教训在本项目出现过好几次（128 项全绿却漏掉 P0 bug）。
      //    ★ 验证完立刻恢复，不影响后续用例。
      const sanity = await page.evaluate(() => {
        const card = document.querySelector('.card.lift');
        if (!card) return { skip: true };
        card.classList.remove('lift');
        const menu = document.querySelector('#stageList .msel.open .mmenu');
        const mr = menu.getBoundingClientRect();
        let blocked = 0;
        menu.querySelectorAll('.mo').forEach(btn => {
          const r = btn.getBoundingClientRect();
          const y = r.top + r.height / 2;
          if (y < mr.top + 1 || y > mr.bottom - 1) return;
          const hit = document.elementFromPoint(r.left + r.width / 2, y);
          if (!hit || (hit !== btn && !btn.contains(hit))) blocked++;
        });
        card.classList.add('lift');                 // 立刻恢复
        return { blocked };
      });
      if (sanity.skip) {
        ok('★ 反向验证（撤销修复后应能检出遮挡）', true, '未找到 .card.lift，本次跳过');
      } else {
        ok('★ 反向验证：撤销修复后确实检出遮挡（证明断言有效）',
          sanity.blocked > 0, `检出 ${sanity.blocked} 项被挡`);
      }
      // 选中一项
      await page.click('#stageList .msel.open .mo[data-v="sensevoice"]');
      await page.waitForTimeout(450);
      const after = await page.$eval('#stageList .msel[data-act="engine"]',
        el => ({ v: el.dataset.value,
                 text: el.querySelector('.mv').textContent.replace(/\s+/g, ' ').trim(),
                 set: el.classList.contains('set') }));
      ok('★ 选中后触发器文案更新且高亮', after.v === 'sensevoice' && after.set &&
        /SenseVoice（适合中文）/.test(after.text), after.text);
      ok('选中后浮层自动关闭', !(await page.isVisible('#stageList .msel.open .mmenu')));
      // 点页面其他地方也要能关闭
      await page.click('#stageList .msel[data-act="engine"]');
      await page.waitForTimeout(350);
      await page.click('#stageTitle, .pagehead h2').catch(() => {});
      await page.waitForTimeout(400);
      ok('★ 点外部可关闭浮层', !(await page.isVisible('#stageList .msel.open .mmenu')));

      // ★ 多个文件时：同时只能展开一个菜单。
      //   浮层 DOM 是常驻的（只靠 display 切换），所以选择器必须限定 .open，
      //   否则会点到隐藏的那个 —— 这个坑在走查时真踩到过。
      await page.setInputFiles('#filepick',
        'C:/Users/Eli/WorkBuddy/workbuddy/soundscribe/m1/asr_example_zh.wav');
      await page.waitForTimeout(3000);
      const all = await page.$$('#stageList .msel[data-act="engine"]');
      ok('可以有多个文件各带一个下拉', all.length >= 2, all.length + ' 个');
      // 先记住第一行当前的引擎值，稍后验证它**没有被误改**
      // （不能硬编码成 'auto' —— 前面的步骤可能已经把它改过）
      const firstBefore = await page.$eval('#stageList .msel', el => el.dataset.value);
      await all[0].click(); await page.waitForTimeout(350);
      ok('第一行展开', (await page.$$eval('#stageList .msel.open', els => els.length)) === 1);
      // ★ 关键：第一行菜单开着时，**直接点第二行的下拉**要能一步切过去，
      //   并且绝不能穿透去选中第一行的选项（那会静默改错行）。
      //   做法是浮层向左展开 → 压不到任何控件。
      await all[1].click(); await page.waitForTimeout(450);
      const afterCross = await page.evaluate(() => ({
        opened: document.querySelectorAll('#stageList .msel.open').length,
        which: [...document.querySelectorAll('#stageList .msel')]
                 .findIndex(el => el.classList.contains('open')),
        first: document.querySelectorAll('#stageList .msel')[0].dataset.value,
      }));
      ok('★ 一步切到第二行（第一行自动收起）',
        afterCross.opened === 1 && afterCross.which === 1,
        '展开 ' + afterCross.opened + ' 个，第 ' + (afterCross.which + 1) + ' 行');
      ok('★ 没有误改第一行的引擎（点击前后不变）',
        afterCross.first === firstBefore,
        '点击前 ' + firstBefore + ' → 点击后 ' + afterCross.first);
      const opened = await page.$$eval('#stageList .msel.open', els => els.length);
      ok('★ 同时只展开一个菜单（不叠层）', opened === 1, opened + ' 个展开');
      ok('★ 隐藏的菜单不会被误当成可点',
        (await page.$$eval('#stageList .msel.open .mmenu .mo', els => els.length)) >= 5);
      await all[1].click(); await page.waitForTimeout(350);   // 收起
      // 清理
      while ((await page.$$('#stageList button[data-act="remove"]')).length) {
        await page.click('#stageList button[data-act="remove"]');
        await page.waitForTimeout(500);
      }
    }

    /* ═══ 4.5 实时识别文字行 ═══ */
    console.log('\n【4.5】实时识别文字行（进度条下方滚动那行）');
    {
      ok('有实时文字行元素', await page.isVisible('#jobLive'));
      // 任务跑过一段时间后，那一行应该已经有识别出来的内容
      let live = '', pulse = false;
      for (let k = 0; k < 30; k++) {
        const r = await page.evaluate(() => ({
          t: (document.getElementById('jobLive') || {}).textContent || '',
          on: (document.getElementById('jobLiveWrap') || {className:''}).className.includes('on'),
        }));
        live = r.t; pulse = r.on;
        if (live.length > 12 && !/准备中|等待识别/.test(live)) break;
        await page.waitForTimeout(1500);
      }
      ok('★ 转写过程中实时行会不断刷新出识别文字',
        live.length > 12 && !/准备中|等待识别/.test(live), live.slice(-40));
      ok('★ 识别中显示脉冲指示', pulse || live.length > 12, pulse ? '● 亮' : '（已结束）');
      const box = await page.evaluate(() => {
        const el = document.getElementById('jobLive');
        return el ? getComputedStyle(el).textAlign : '';
      });
      // 右对齐：新出现的字永远在可见侧
      ok('★ 文字右对齐（新字从右侧出现，不会被截断在右边）', box === 'right', box);
    }

    /* ═══ 4.6 能力矩阵：生效范围的唯一来源 ═══ */
    console.log('\n【4.6】能力矩阵与设置回执');
    {
      // ① 矩阵自身必须自洽（声明支持却没写注入方式 / 漏写引擎，都会被 audit 抓出来）
      const capApi = await fetch(BASE + '/api/capabilities').then(r => r.json());
      ok('★ 能力矩阵自检通过（无自相矛盾）', capApi.ok === true,
        (capApi.problems || []).map(p => p.cap + ':' + p.type).join(' / ') || 'problems=0');
      const caps = capApi.capabilities || [];
      ok('矩阵覆盖了引擎级与后处理级能力', caps.length >= 8, caps.length + ' 项');

      // ② 工作台的 VAD 说明必须由矩阵生成 —— 手写的那份会过期
      const vadTxt = (await page.textContent('#vadScopeNote')).replace(/\s+/g, ' ').trim();
      ok('★ VAD 生效范围已由矩阵填充（不是占位符）',
        vadTxt.length > 10 && !/加载中/.test(vadTxt), vadTxt.slice(0, 60));
      ok('★ VAD 说明如实写明对 sherpa 引擎不生效',
        /Whisper/.test(vadTxt) && /不支持|不受/.test(vadTxt), vadTxt.slice(0, 70));

      // ③ 引擎下拉里必须标出"这台引擎上哪些设置不生效"（选择之前就该知道）
      await page.click('#stageList .msel[data-act="engine"]').catch(() => {});
      await page.waitForTimeout(150);
      await page.setInputFiles('#filepick',
        'C:/Users/Eli/WorkBuddy/workbuddy/soundscribe/m1/asr_example_zh.wav');
      await page.waitForTimeout(2500);
      await page.click('#stageList .msel[data-act="engine"]');
      await page.waitForTimeout(450);
      const mnos = await page.$$eval('#stageList .msel.open .mmenu .mo .mno',
        els => els.map(e => e.textContent.replace(/\s+/g, ' ').trim()));
      ok('★ 引擎选项里标注了"以下设置对它无效"', mnos.length >= 1,
        mnos.slice(0, 3).join(' | ').slice(0, 100));
      ok('★ 标注内容来自矩阵（点名具体设置，不是笼统一句）',
        mnos.some(t => /VAD|人声检测/.test(t)), mnos[0] ? mnos[0].slice(0, 60) : '');
      await page.keyboard.press('Escape');
      await page.waitForTimeout(300);

      // ④ 设置回执：任务结束后必须能读到"这趟实际生效了什么"
      if (await page.$('#stageList button[data-act="remove"]')) {
        while ((await page.$$('#stageList button[data-act="remove"]')).length) {
          await page.click('#stageList button[data-act="remove"]');
          await page.waitForTimeout(400);
        }
      }
      let done = false;
      for (let i = 0; i < 40; i++) {
        const t = (await page.textContent('#jobStats')).trim();
        if (t.includes('已完成') || t.includes('失败')) { done = t.includes('已完成'); break; }
        await page.waitForTimeout(1500);
      }
      const jr = await fetch(BASE + '/api/jobs').then(r => r.json());
      const finished = (jr.jobs || []).find(j => j.status === 'done' && j.applied
        && (j.applied.applied || []).length);
      ok('★ 任务回执记录了实际生效的设置（接口层）',
        !!finished, finished ? (finished.applied.applied.length + ' 项生效，引擎 '
          + finished.applied.engine) : '未找到带回执的已完成任务');
      if (finished) {
        const ap = finished.applied;
        ok('★ 回执写明每项传成了什么（可人工核对）',
          ap.applied.every(a => a.param && a.form !== undefined),
          ap.applied.map(a => a.label + '→' + a.param).join(' / ').slice(0, 90));
        // 不生效的项必须给出原因，不能只丢一个"不支持"
        ok('★ 被忽略的设置都带了原因',
          (ap.ignored || []).every(i => (i.why || '').length > 0),
          (ap.ignored || []).map(i => i.label).join('、') || '无被忽略项');
      }
      if (done) {
        ok('任务完成后界面上出现设置回执', await page.isVisible('#jobReceipt'));
        const rows = await page.$$eval('#jobReceipt .rctrow', els => els.length);
        ok('回执里有生效条目', rows >= 1, rows + ' 条');
      }
    }

    /* ═══ 4.7 中文任务（SenseVoice）的设置回执 ═══ */
    // ★ 为什么单独测这一段：SenseVoice 对热词/VAD/批处理**全都不支持**，
    //   所以它的回执是"0 项生效 + N 项不生效"。而回执块的显示条件曾经写成
    //   `applied.length > 0` → 整块被隐藏 → 用户填了一堆热词、跑完中文转写，
    //   界面上什么都没说，他会以为热词生效了。
    //   "0 项生效"恰恰是最该显示的一句话。
    console.log('\n【4.7】中文任务（SenseVoice）也必须看得到回执');
    {
      await page.setInputFiles('#filepick',
        'C:/Users/Eli/WorkBuddy/workbuddy/soundscribe/m1/asr_example_zh.wav');
      await page.waitForTimeout(3000);
      const startBtn = await page.$('#stageList .srow button[data-act="start"]');
      ok('中文素材已入队', startBtn !== null);
      if (startBtn) {
        await startBtn.click();
        await page.waitForTimeout(3000);
        let done = false;
        for (let i = 0; i < 60; i++) {
          const t = (await page.textContent('#jobStats')).trim();
          if (t.includes('已完成')) { done = true; break; }
          if (t.includes('失败')) break;
          await page.waitForTimeout(1000);
        }
        ok('中文任务跑完', done, (await page.textContent('#jobStats')).trim().slice(0, 40));
        if (done) {
          const applied = await page.evaluate(async () => {
            const r = await fetch('/api/jobs').then(x => x.json());
            const j = (r.jobs || []).find(x => x.status === 'done' && x.engine === 'sensevoice');
            return j ? j.applied : null;
          });
          ok('★ 中文任务路由到 SenseVoice',
            !!(applied && applied.engine === 'sensevoice'),
            applied ? applied.engine_display : '未找到');
          ok('★ SenseVoice 任务的回执块可见（0 项生效也要显示）',
            await page.isVisible('#jobReceipt'));
          const rtxt = (await page.textContent('#jobReceipt')).replace(/\s+/g, ' ').trim();
          ok('★ 回执如实列出"哪些设置对它不生效"',
            /不生效/.test(rtxt) && /热词/.test(rtxt), rtxt.slice(0, 100));
        }
      }
    }

    console.log('\n【5】词库页（含信息架构回归）');
    // ★ 这一组断言是给"信息架构"上锁：卡片被搬错页面时立刻报出来。
    //   背景：2026-09-30 做了一次重组（词库改名、导出设置挪到文稿库、
    //   转写选项挪到工作台、磁盘占用挪到侧边栏），结构容易在后续改动中被搬回去。
    {
      const navLabel = (await page.textContent('#nav button[data-view="settings"]')).trim();
      ok('★ 导航第 4 项已改名为「词库」', navLabel.includes('词库') && !navLabel.includes('导出'),
        navLabel);

      const inView = async (view, sel) => page.evaluate(([v, q]) => {
        const root = document.getElementById('view-' + v);
        return !!(root && root.querySelector(q));
      }, [view, sel]);

      // 转写选项仍在工作台（用时间轴/VAD 这些还在的控件判断 ——
      // 原来用 #optEngine 判断，但引擎选择已从这里移除）
      ok('★ 转写选项在工作台（决定「怎么转」的地方）',
        await inView('workbench', '#optTimeline') && await inView('workbench', '#optVad'));
      ok('★ 导出设置在文稿库（真正导出成果的地方）',
        await inView('library', '#optFormats'));
      ok('★ 词库页只剩词库相关（纠错规则 + 热词库）',
        await inView('settings', '#corrList'));
      ok('★ 词库页不再含导出设置 / 磁盘占用',
        !(await inView('settings', '#optFormats')) && !(await inView('settings', '#btnCleanSpace')));

      // ★ 引擎选择现在**只在待开始列表里**（2026-09-30 定案）：
      //   转写选项里那个重复的引擎选择已移除，避免两处打架。
      ok('★ 转写选项里不再有引擎选择（只保留待开始一处）',
        !(await inView('workbench', '#optEngine')));
      ok('★ 转写选项里有"引擎在待开始选"的说明',
        (await page.textContent('#view-workbench')).includes('待开始'));

      // 系统状态必须常驻侧边栏 —— 切到任意页面都看得见
      let alwaysVisible = true;
      for (const v of ['workbench', 'library', 'models', 'settings', 'diag']) {
        await page.click(`#nav button[data-view="${v}"]`);
        await page.waitForTimeout(450);
        if (!(await page.isVisible('#storageHint')) || !(await page.isVisible('#btnCleanSpace'))) {
          alwaysVisible = false; break;
        }
      }
      ok('★ 系统状态（磁盘 + 释放空间）在任意页面都可见', alwaysVisible, '侧边栏常驻');
      await page.click('#nav button[data-view="settings"]');
      await page.waitForTimeout(600);

      // ★★ 生效范围卡片必须由能力矩阵生成 —— 这组断言是给"不许再手写生效范围"上锁。
      //   背景：这张卡片以前是手写的，实测漂移过两次：
      //     ① 写着"领域热词 仅 Whisper 引擎生效"，而 Parakeet 其实已经接上了原生热词；
      //     ② 写着"内置规则来自 Whisper"，而这句话的事实来源（source_engine 字段）当时
      //        根本没记录在数据里 —— 等于把一个猜测写成了结论。
      const items = await page.$$eval('#scopeBody .scopeitem', els => els.length);
      ok('★ 生效范围卡片由矩阵生成', items >= 2, items + ' 块');
      const scopeTxt = (await page.textContent('#scopeBody')).replace(/\s+/g, ' ').trim();
      ok('★ 卡片不再写死「仅 Whisper 引擎生效」',
        !/仅 Whisper 引擎生效/.test(scopeTxt), scopeTxt.slice(0, 60));
      const hotTag = await page.$$eval('#scopeBody .scopeitem', els => els.map(e => {
        const t = e.querySelector('.scopetitle');
        return t ? t.textContent.replace(/\s+/g, ' ').trim() : '';
      }));
      ok('★ 热词的生效范围如实反映 Parakeet 已接入',
        hotTag.some(t => /领域热词/.test(t) && /Parakeet/.test(t)),
        hotTag.join(' | '));
      ok('★ 纠错规则表标注为全部引擎生效',
        hotTag.some(t => /纠错规则表/.test(t) && /全部引擎生效/.test(t)),
        hotTag.join(' | '));
      // 标定来源提示必须来自数据（corrections.calibration），不是手写的一句
      const calApi = await fetch(BASE + '/api/corrections').then(r => r.json());
      ok('★ 纠错规则记录了标定来源引擎',
        !!(calApi.calibration && Object.keys(calApi.calibration.by_source_engine || {}).length),
        JSON.stringify(calApi.calibration && calApi.calibration.by_source_engine));
      if (calApi.calibration && calApi.calibration.warn) {
        const wl = await page.$$eval('#scopeBody .warnline', els => els.length);
        ok('★ 标定来源与当前默认引擎不一致时有醒目提示', wl >= 1, wl + ' 条');
      }
    }

    await page.click('#nav button[data-view="settings"]');
    await page.waitForTimeout(2200);
    ok('纠错规则表已渲染',
      (await page.$$eval('#corrList .rrow', els => els.length)) > 0,
      (await page.$$eval('#corrList .rrow', els => els.length)) + ' 行');
    ok('热词库已渲染',
      (await page.$$eval('#hotList .item', els => els.length)) > 0,
      (await page.$$eval('#hotList .item', els => els.length)) + ' 个词');
    const scrollBox = await page.$eval('#corrList', el => getComputedStyle(el).maxHeight);
    ok('纠错列表是限高滚动区（不会把页面撑长）', scrollBox !== 'none', 'max-height: ' + scrollBox);
    ok('★ 内置规则也有删除入口',
      await page.$('#corrList [data-hide-rule]') !== null,
      (await page.$$eval('#corrList [data-hide-rule]', e => e.length)) + ' 个');
    ok('有「恢复内置规则」按钮', await page.$('#btnCorrRestore') !== null);
    ok('有「恢复内置热词」按钮', await page.$('#btnHotRestore') !== null);

    // 过滤框
    const totalBefore = await page.$$eval('#corrList .rrow', e => e.length);
    await page.fill('#corrFilter', '手势');
    await page.waitForTimeout(600);
    const totalAfter = await page.$$eval('#corrList .rrow', e => e.length);
    ok('过滤框生效', totalAfter < totalBefore, totalBefore + ' 行 → ' + totalAfter + ' 行');
    await page.fill('#corrFilter', '');
    await page.waitForTimeout(500);

    // 内置规则移除 → 恢复
    const before = await page.$$eval('#corrList .rrow', e => e.length);
    page.once('dialog', d => d.accept());
    await page.click('#corrList [data-hide-rule]');
    await page.waitForTimeout(1500);
    const after = await page.$$eval('#corrList .rrow', e => e.length);
    ok('★ 内置规则可以移除', after === before - 1, before + ' 行 → ' + after + ' 行');
    page.once('dialog', d => d.accept());
    await page.click('#btnCorrRestore');
    await page.waitForTimeout(1500);
    const restored = await page.$$eval('#corrList .rrow', e => e.length);
    ok('★ 移除的内置规则可以恢复', restored === before, after + ' 行 → ' + restored + ' 行');

    /* ═══ 6. 模型中心 ═══ */
        /* ═══ 5.5 释放空间（逐项勾选 + 二次确认） ═══ */
    console.log('\n【5.5】释放空间');
    await page.click('#nav button[data-view="settings"]');
    await page.waitForTimeout(1500);
    ok('有「释放空间」按钮', await page.$('#btnCleanSpace') !== null);

    const activeCount = async () => page.evaluate(async () => {
      try { const d = await (await fetch('/api/jobs')).json(); return d.active_count || 0; }
      catch (_) { return 0; }
    });

    // ★ 保护：有任务在跑时必须拒绝清理（清缓存会打断它、删模型会让它失败）
    //
    // ⚠️ 这里有个时序竞争：任务可能在"判断忙碌"与"点击"之间就结束了，
    //    那弹层合法地打开，断言会假失败（第一次就是踩到这个）。
    //    所以断言必须按**点击之后**的真实忙碌状态来判 —— 只有点击后仍然忙着，
    //    才有资格断言"应该被拒绝"。
    if (await activeCount() > 0) {
      await page.click('#btnCleanSpace');
      await page.waitForTimeout(1200);
      const openedWhileBusy = await page.isVisible('#cleanModal');
      const busyAfterClick = await activeCount() > 0;
      if (busyAfterClick) {
        const toastTxt = await page.textContent('#toasts').catch(() => '');
        ok('★ 有任务在跑时拒绝清理', !openedWhileBusy && /任务/.test(toastTxt),
          openedWhileBusy ? '弹层竟然打开了' : (toastTxt || '').trim().slice(-40));
      } else {
        ok('★ 有任务在跑时拒绝清理', true,
          '（任务在点击瞬间结束了，弹层合法打开；本轮跳过此项）');
      }
      if (openedWhileBusy) {          // 恢复了就关掉，别影响后续步骤
        await page.click('#cleanCancel');
        await page.waitForTimeout(600);
      }
    } else {
      ok('★ 有任务在跑时拒绝清理', true, '（本次没有活动任务，跳过此项）');
    }

    // 等任务跑完（清理要求空闲）
    for (let k = 0; k < 45; k++) {
      if (await activeCount() === 0) break;
      await page.waitForTimeout(1500);
    }

    await page.click('#btnCleanSpace');
    await page.waitForTimeout(2500);
    ok('空闲时弹层打开', await page.isVisible('#cleanModal'));
    const cleanRows = await page.$$eval('#cleanList .cleanrow', els => els.map(e => ({
      id: e.dataset.id,
      checked: e.querySelector('input').checked,
      disabled: e.querySelector('input').disabled,
    })));
    ok('列出可清理项', cleanRows.length >= 4, cleanRows.map(r => r.id).join(' / '));
    ok('★ 成果文件默认不勾选', cleanRows.find(r => r.id === 'exports')
      ? cleanRows.find(r => r.id === 'exports').checked === false : false, 'exports');
    ok('★ 模型默认不勾选（删了要重下）', cleanRows.find(r => r.id === 'models')
      ? cleanRows.find(r => r.id === 'models').checked === false : false, 'models');

    // 勾上「导出结果」，按钮文案与状态色都应转为警示
    await page.evaluate(() => {
      const cb = [...document.querySelectorAll('#cleanList [data-clean]')].find(x => x.dataset.clean === 'exports');
      if (cb && !cb.disabled) { cb.checked = true; cb.dispatchEvent(new Event('change', { bubbles: true })); }
    });
    await page.waitForTimeout(400);
    const goLabel = (await page.textContent('#cleanGo')).trim();
    ok('★ 勾选成果文件后按钮文案带警告', /成果|确认删除/.test(goLabel), goLabel);
    const stCls = (await page.getAttribute('#cleanStatus', 'class')) || '';
    ok('★ 选中成果文件时状态文字转为警示色', /bad/.test(stCls), stCls);

    // 全部取消勾选 → 按钮禁用
    await page.evaluate(() => {
      document.querySelectorAll('#cleanList [data-clean]').forEach(cb => {
        cb.checked = false; cb.dispatchEvent(new Event('change', { bubbles: true }));
      });
    });
    await page.waitForTimeout(400);
    const goDisabled = await page.getAttribute('#cleanGo', 'disabled');
    ok('★ 一项都不选时无法执行', goDisabled !== null && goDisabled !== undefined, 'disabled=' + goDisabled);

    if (await page.isVisible('#cleanModal')) {
      await page.click('#cleanCancel');
      await page.waitForTimeout(700);
    }
    ok('取消后弹层关闭', !(await page.isVisible('#cleanModal')));

/* ═══ 5.6 侧边栏系统状态 ═══ */
console.log('\n【5.6】侧边栏系统状态');
{
  ok('磁盘占用已上侧边栏', await page.isVisible('#storageHint'));
  const diskTxt = (await page.textContent('#storageHint')).trim();
  ok('★ 磁盘剩余已填充（不是占位符）', /\d/.test(diskTxt) && !/读取中/.test(diskTxt), diskTxt);
  const leg = await page.$$eval('#storageLegend li', els => els.map(e => e.textContent.replace(/\s+/g,'')));
  ok('列出占用构成', leg.length >= 4, leg.join(' / '));
  ok('有占用堆叠条', (await page.$$eval('#storageStack i', els => els.length)) >= 1);
  ok('释放空间入口常驻侧边栏', await page.isVisible('#btnCleanSpace'));
}

console.log('\n【6】模型中心');
    await page.click('#nav button[data-view="models"]');
    await page.waitForTimeout(2200);
    const mrows = await page.$$eval('#modelList .mrow', els => els.length);
    ok('模型清单已渲染', mrows >= 6, mrows + ' 个');

    // ★ 按机器推荐：结论条 + 分组 + 推荐徽标 + 两源下载按钮
    const recoTxt = (await page.textContent('#recoBar')).replace(/\s+/g, ' ').trim();
    ok('★ 有「为你推荐」结论条', recoTxt.length > 10, recoTxt.slice(0, 56));
    const groups = await page.$$eval('#modelList .mgroup', els => els.map(e => e.textContent.trim()));
    ok('★ 模型按用途分组', groups.length >= 2, groups.join(' / '));
    ok('★ 每个分组各有推荐款',
      (await page.$$eval('#modelList .tag.reco', els => els.length)) >= 2,
      (await page.$$eval('#modelList .tag.reco', els => els.length)) + ' 个徽标');
    ok('★ 推荐款给出理由', (await page.$$eval('#modelList .reco-why', els => els.length)) >= 1);
    const dlb = await page.$$eval('#modelList .dlbtns', els => els.map(g => ({
      n: g.querySelectorAll('button').length,
      hint: ((g.querySelector('.dlhint') || {}).textContent || '').trim(),
      labels: [...g.querySelectorAll('button')].map(b => b.textContent.replace(/\s+/g, ' ').trim()),
    })));
    if (dlb.length) {
      ok('★ 未安装模型有两个下载源按钮', dlb[0].n === 2, dlb[0].labels.join('  |  '));
      ok('★ 镜像按钮标注「默认 · 中国用户」', /中国用户/.test(dlb[0].hint), dlb[0].hint);
    } else {
      ok('★ 未安装模型有两个下载源按钮', true, '（本机模型都已安装，跳过）');
      ok('★ 镜像按钮标注「默认 · 中国用户」', true, '（跳过）');
    }
    ok('有下载按钮', await page.$('#modelList [data-dl]') !== null);
    ok('已装模型有删除按钮', await page.$('#modelList [data-del]') !== null);
    ok('量化档三态标签已显示',
      (await page.$$eval('#modelList .var', els => els.length)) > 0,
      (await page.$$eval('#modelList .var', els => els.length)) + ' 个');

    /* ═══ 7. 诊断体检 ═══ */
    console.log('\n【7】诊断与环境体检');
    await page.click('#nav button[data-view="diag"]');
    await page.waitForTimeout(7000);
    const verdict = (await page.textContent('#healthVerdict')).trim();
    ok('体检结论已显示', verdict.length > 3 && !verdict.includes('正在检查'), verdict);
    const hrows = await page.$$eval('#healthList .hrow', els => els.length);
    ok('逐项检查清单已渲染', hrows >= 15, hrows + ' 项');
    const marks = await page.$$eval('#healthList .hrow .mk', els => els.map(e => e.textContent.trim()));
    // — 是「可选未装」（未安装的模型），不是问题标记
    ok('每项都有状态标记（✓ / ! / ✕ / —）',
      marks.length === hrows && marks.every(m => ['✓', '!', '✕', '—'].includes(m)),
      '✓×' + marks.filter(m => m === '✓').length
      + ' !×' + marks.filter(m => m === '!').length
      + ' —×' + marks.filter(m => m === '—').length);
    const stats = await page.$$eval('#healthStats .metric', els =>
      els.map(e => e.querySelector('.k').textContent.trim() + '=' + e.querySelector('.v').textContent.trim()));
    ok('「未安装模型」单独归为「待装模型」', stats.some(s => s.startsWith('待装模型=')), stats.join(' '));
    const problemN = parseInt((stats.find(s => s.startsWith('问题项=')) || '=0').split('=')[1], 10) || 0;
    const vClass = (await page.getAttribute('#healthVerdict', 'class')) || '';
    ok('未安装模型不会让结论变黄/红',
      problemN > 0 ? true : !/warn|bad/.test(vClass),
      '问题项=' + problemN + ' 结论样式="' + vClass + '"');
    ok('没有待修问题时，提示指向模型下载',
      problemN > 0 ? true : /模型/.test((await page.textContent('#healthFixNote')) || ''),
      ((await page.textContent('#healthFixNote')) || '').trim().slice(0, 46));
    ok('有「诊断修复」按钮', await page.$('#btnHealthFix') !== null);
    const fixLabel = (await page.textContent('#btnHealthFix')).trim();
    ok('修复按钮显示可修项数量', /诊断修复/.test(fixLabel), fixLabel);
    ok('原始报告已生成',
      (await page.textContent('#diagBox')).length > 200,
      (await page.textContent('#diagBox')).length + ' 字符');

    /* ═══ 8. 主题与响应式 ═══ */
    console.log('\n【8】主题与响应式');
    await page.click('#themeswitch button[data-set="day"]');
    await page.waitForTimeout(700);
    const theme = await page.getAttribute('html', 'data-theme');
    ok('切换到浅色主题', theme === 'day', theme);
    await page.click('#themeswitch button[data-set="space"]');
    await page.waitForTimeout(500);
    ok('切回深空主题', (await page.getAttribute('html', 'data-theme')) === 'space');

    for (const [w, h, label] of [[3840, 2160, '4K'], [1920, 1080, 'FHD'], [1366, 768, '小笔记本']]) {
      await page.setViewportSize({ width: w, height: h });
      await page.waitForTimeout(600);
      const overflow = await page.evaluate(() =>
        document.documentElement.scrollWidth > document.documentElement.clientWidth + 2);
      const rootFs = await page.evaluate(() => getComputedStyle(document.documentElement).fontSize);
      ok(w + '×' + h + '（' + label + '）无横向溢出', !overflow, '根字号 ' + rootFs);
    }
    await page.setViewportSize({ width: 1600, height: 1000 });

  } catch (e) {
    console.log('\n★ 测试过程中抛出异常：' + e.message);
    fail++;
    failures.push('异常：' + e.message);
  }

  /* ═══ 错误汇总 ═══ */
  console.log('\n【前端错误检查】');
  ok('无 JS 运行时错误', jsErrors.length === 0, jsErrors.slice(0, 3).join(' | ') || '无');
  const realNet = netErrors.filter(e => !/keepalive/.test(e));
  ok('无失败的网络请求', realNet.length === 0, realNet.slice(0, 4).join(' | ') || '无');

  console.log('\n' + '═'.repeat(58));
  console.log('  通过 ' + pass + ' 项，失败 ' + fail + ' 项');
  if (failures.length) {
    console.log('  失败清单：');
    failures.forEach(f => console.log('    · ' + f));
  }
  console.log('═'.repeat(58));

  await browser.close();
  await cleanupJobs('收尾');
  process.exit(fail ? 1 : 0);
})();
