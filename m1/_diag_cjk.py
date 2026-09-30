import json, re
from collections import Counter

s = json.load(open('mix3-sensevoice.json', encoding='utf-8'))
st = s['text']
raw = s['raw_text']
CJK = re.compile(r'[\u4e00-\u9fff]')

chars = CJK.findall(st)
print('中文总字符', len(chars), '| 不重复', len(set(chars)))
print()
print('频次 top20:')
print('  ' + '  '.join(c + ':' + str(n) for c, n in Counter(chars).most_common(20)))
print()
marker = '<' + '|'
print('rich 标记残留数:', st.count(marker))
plain = re.findall(r'<\|[^|]*\|>', raw)
print('raw 里的标记种类:', Counter(plain).most_common(12))
print()

idxs = [m.start() for m in CJK.finditer(st)]
if idxs:
    print('中文首现位置: %d / %d (%.0f%%)' % (idxs[0], len(st), idxs[0] / len(st) * 100))
    print('中文末现位置: %d / %d (%.0f%%)' % (idxs[-1], len(st), idxs[-1] / len(st) * 100))
blocks = re.findall(r'[\u4e00-\u9fff]{2,}', st)
print('连续中文块(>=2字):', len(blocks), '最长', max((len(b) for b in blocks), default=0))
print('块长分布:', Counter(len(b) for b in blocks).most_common())

# 中文字符与相邻 ASCII 的关系：判断是"孤立插入"还是"整段切换语言"
iso = 0
for m in CJK.finditer(st):
    i = m.start()
    left = st[max(0, i - 3):i]
    right = st[i + 1:i + 4]
    if re.search(r'[A-Za-z]', left) or re.search(r'[A-Za-z]', right):
        iso += 1
print()
print('孤立插入(紧邻英文字母)的中文字符数:', iso, '/', len(chars))
