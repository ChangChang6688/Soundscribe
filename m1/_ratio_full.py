import json, re, glob, os

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LATIN_WORD = re.compile(r"[A-Za-z]+")


def ratio(text):
    cjk = len(CJK.findall(text))
    latin = LATIN_WORD.findall(text)
    units = cjk + len(latin)
    return cjk, len(latin), (cjk / units if units else 0.0)


files = [
    ("中文授课 122 分钟", "8.22-whisper-turbo.json", "segments_detail"),
    ("英文讲解 8.2 分钟", "en-whisper-turbo.json", "segments_detail"),
    ("英文新闻 3 分钟", "mix3-whisper.json", "segments_detail"),
]

print(f"{'素材':<20}{'中文字符':>10}{'英文词':>9}{'中文占比':>10}{'判定':>14}")
print("-" * 66)
for label, fn, key in files:
    if not os.path.exists(fn):
        continue
    d = json.load(open(fn, encoding="utf-8"))
    text = " ".join(x["text"] for x in d[key])
    cjk, latin, r = ratio(text)
    if r > 0.85:
        verdict = "SenseVoice"
    elif r >= 0.40:
        verdict = "中英夹杂→Whisper"
    else:
        verdict = "Whisper"
    print(f"{label:<20}{cjk:>10}{latin:>9}{r*100:>9.1f}%{verdict:>14}")

print()
print("=== 中文授课素材里的英文词 top20（看它夹了多少英文）===")
d = json.load(open("8.22-whisper-turbo.json", encoding="utf-8"))
text = " ".join(x["text"] for x in d["segments_detail"])
from collections import Counter

words = [w.lower() for w in LATIN_WORD.findall(text)]
print("  " + "  ".join(f"{w}:{n}" for w, n in Counter(words).most_common(20)))
print()
print("英文词总出现次数:", len(words), "| 不重复:", len(set(words)))
