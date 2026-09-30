/* 前端一致性校验：语法、标签闭合、JS 引用的 id 是否都存在于 HTML、重复绑定 */
const fs = require('fs');
const path = require('path');

const dir = path.resolve(__dirname, '..', 'app', 'web');
const js = fs.readFileSync(path.join(dir, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(dir, 'app.css'), 'utf8');

let bad = 0;
try { new Function(js); console.log('✓ app.js 语法 OK  (' + (js.length / 1024).toFixed(1) + ' KB)'); }
catch (e) { bad++; console.log('✗ app.js 语法错误: ' + e.message); }

for (const [tag, open, close] of [
  ['div', /<div/g, /<\/div>/g],
  ['section', /<section/g, /<\/section>/g],
  ['details', /<details/g, /<\/details>/g],
]) {
  const a = (html.match(open) || []).length, b = (html.match(close) || []).length;
  if (a === b) console.log('✓ ' + tag + ' 开/闭平衡 (' + a + ')');
  else { bad++; console.log('✗ ' + tag + ' 不平衡: ' + a + ' / ' + b); }
}

// ★ 排除注释行再提取：注释里写示例（比如 `$('#x')`）会被当成真实引用，
//   产生"JS 引用但 HTML 缺失的 id: x"这种假报错。检查器自己得先分清代码和注释。
const jsCode = js.split('\n')
  .filter(line => {
    const t = line.trim();
    return !(t.startsWith('//') || t.startsWith('*') || t.startsWith('/*'));
  })
  .join('\n');

const ids = [...new Set([...jsCode.matchAll(/\$\('#([A-Za-z0-9_-]+)'\)/g)].map(m => m[1]))];
const missing = ids.filter(i => !html.includes('id="' + i + '"'));
if (missing.length) { bad++; console.log('✗ JS 引用但 HTML 缺失的 id: ' + missing.join(', ')); }
else console.log('✓ JS 引用的 ' + ids.length + ' 个 id 全部存在于 HTML');

const binds = [...jsCode.matchAll(/\$\('#([A-Za-z0-9_-]+)'\)\.addEventListener/g)].map(m => m[1]);
const cnt = {};
binds.forEach(i => { cnt[i] = (cnt[i] || 0) + 1; });
const dup = Object.entries(cnt).filter(([, v]) => v > 1);
if (dup.length) { bad++; console.log('✗ 重复绑定: ' + dup.map(x => x[0] + '×' + x[1]).join(', ')); }
else console.log('✓ 无重复事件绑定');

// ★ 类名撞车检查（收窄版）
//   只看「模板拼接出来的修饰类」—— 那才是我新编的名字，有撞车风险。
//   静态写的 class="btn" / class="metric" 是**故意**用全局类，不该报。
//   实例：给清理行的"无数据"状态拼了 'empty'，撞上全局 .empty 的 text-align:center，
//        整行文字被居中，代码不报错、只有肉眼看才发现。
{
  const GLOBAL_UTIL = ['empty', 'note', 'hint', 'pill', 'metric', 'card', 'btn', 'item', 'dot'];
  const suspects = new Set();
  for (const m of js.matchAll(/class="([^"]*\$\{[^"]*)"/g)) {
    const attr = m[1];
    for (const lit of attr.matchAll(/'([A-Za-z][\w-]*)'/g)) suspects.add(lit[1]);
  }
  const clash = [...suspects].filter(c => GLOBAL_UTIL.includes(c));
  if (clash.length) {
    console.log('⚠ 模板拼出的修饰类与全局工具类同名（可能静默改样式）: ' + clash.join(', '));
  } else {
    console.log('✓ 模板拼出的修饰类未与全局工具类撞名');
  }
}

for (const cls of ['.hrow.optional .mk', '.note.ok', '.verdict.ok', '.cleanrow.zero']) {
  const sel = cls.replace(/\./g, '\\.').replace(/ /g, '\\s*');
  if (new RegExp(sel).test(css.replace(/\n/g, ' '))) console.log('✓ 样式存在: ' + cls);
  else { bad++; console.log('✗ 样式缺失: ' + cls); }
}

console.log('');
console.log(bad ? '共 ' + bad + ' 处问题' : '全部通过');
process.exit(bad ? 1 : 0);
