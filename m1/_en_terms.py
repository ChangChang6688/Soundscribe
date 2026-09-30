import json, re
from collections import Counter

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LATIN_WORD = re.compile(r"[A-Za-z]+")


def load(fn, key):
    d = json.load(open(fn, encoding="utf-8"))
    if key == "segments_detail":
        return " ".join(x["text"] for x in d[key])
    return d["text"]


w = load("8.22-whisper-turbo.json", "segments_detail")
s = load("8.22-sensevoice.json", "text")


def words(t):
    return [x.lower() for x in LATIN_WORD.findall(t)]


W, S = Counter(words(w)), Counter(words(s))
print("=== 中文授课素材里的英文术语：两引擎命中对比 ===")
print()
print(f"{'英文词':<16}{'Whisper':>9}{'SenseVoice':>12}   说明")
print("-" * 60)
for term, n in W.most_common(28):
    m = S.get(term, 0)
    note = ""
    if n >= 3 and m == 0:
        note = "← SenseVoice 漏掉"
    elif m >= n * 2 and n >= 3:
        note = "← SenseVoice 更全"
    print(f"{term:<16}{n:>9}{m:>12}   {note}")

print()
print(f"英文词总出现    Whisper {sum(W.values())} 次 / SenseVoice {sum(S.values())} 次")
print(f"英文词不重复    Whisper {len(W)} 个 / SenseVoice {len(S)} 个")
print()

# 只看出现在中文上下文里的英文术语（长度>=2的实义词）
meaningful = [t for t in W if len(t) >= 3 and W[t] >= 2]
wk = sum(1 for t in meaningful if W[t] > 0)
sk = sum(1 for t in meaningful if S.get(t, 0) > 0)
print(f"高频实义英文术语（>=3字母且出现>=2次）共 {len(meaningful)} 个")
print(f"  Whisper 命中 {wk} 个 / SenseVoice 命中 {sk} 个")

only_w = [t for t in meaningful if S.get(t, 0) == 0]
if only_w:
    print(f"  仅 Whisper 抓到、SenseVoice 漏掉的: {', '.join(only_w[:15])}")
only_s = [t for t in S if len(t) >= 3 and S[t] >= 2 and W.get(t, 0) == 0]
if only_s:
    print(f"  仅 SenseVoice 抓到、Whisper 漏掉的: {', '.join(only_s[:15])}")
