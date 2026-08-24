#!/usr/bin/env bash
# 同一張圖跑 N 次，輸出推論時間的分位數。驗收 spec §9-5。
#
#   scripts/bench.sh /path/to/page.png [次數]
#
# **這個數字決定下一階段的設計**，不是附錄。見 README 的〈怎麼讀 bench 的數字〉。
set -uo pipefail

BASE="${BASE:-http://127.0.0.1:8081}"
IMG="${1:?用法: scripts/bench.sh /path/to/page.png [次數]}"
N="${2:-5}"
[ -f "$IMG" ] || { echo "找不到 $IMG"; exit 2; }

# 找一個真的能跑的 Python。**不能直接用 `python3`** —— Windows 的 Git Bash 上那是
# Microsoft Store 的空殼：找得到、跑起來不輸出任何東西。
PYBIN=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && [ "$("$c" -c 'print(1)' 2>/dev/null)" = 1 ]; then
    PYBIN="$c"; break
  fi
done
[ -n "$PYBIN" ] || { echo "找不到可用的 python（試過 python3 / python / py）"; exit 2; }

TSV="${TMPDIR:-/tmp}/bench.tsv"
echo "圖片: $IMG ($(wc -c < "$IMG") bytes)  次數: $N  目標: $BASE"
: > "$TSV"

for i in $(seq 1 "$N"); do
  body=$(curl -sS -F "file=@$IMG" "$BASE/transcribe")
  line=$(printf '%s' "$body" | "$PYBIN" -c "import json,sys;d=json.load(sys.stdin);t=d.get('timing_ms') or {};print(str(t.get('total','ERR'))+chr(9)+str(t.get('inference','ERR')))" 2>/dev/null || printf 'ERR\tERR')
  printf '%s\n' "$line" >> "$TSV"
  printf '  %2d/%s  total=%s ms  inference=%s ms\n' "$i" "$N" "${line%%$'\t'*}" "${line##*$'\t'}"
done

TSV="$TSV" "$PYBIN" - <<'PY'
import os, statistics as st

rows = [l.split('\t') for l in open(os.environ['TSV']) if l.strip()]
ok = [(int(a), int(b)) for a, b in rows if a.strip().isdigit() and b.strip().isdigit()]
if not ok:
    raise SystemExit("\n沒有成功的樣本 —— 看 docker logs")

def q(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, round((len(v) - 1) * p))]

print(f"\n成功 {len(ok)}/{len(rows)}")
for name, idx in (("total", 0), ("inference", 1)):
    v = [x[idx] for x in ok]
    print(f"  {name:<10} min={min(v)} p50={q(v,.5)} p90={q(v,.9)} max={max(v)} 平均={st.mean(v):.0f} ms")

gap = q([t - i for t, i in ok], .5)
tot = q([t for t, _ in ok], .5)
print(f"\n  total - inference 的 p50 = {gap} ms（佔 total 的 {gap/tot*100:.1f}%）")
note = ("""
  [!] **這個差不是 ONNX session 的冷啟動。** 它只是 FastAPI 的請求開銷（實測約
  10 ms，可以忽略）—— 因為 `inference` 量的是整個 subprocess 的執行時間，而
  session 建立就發生在那裡面。

  要量冷啟動得比較「一個行程處理 N 張」與「N 個行程各處理 1 張」。homr 吃目錄，
  所以在容器裡是：

      docker compose exec homr-worker sh -c 'cp /path/*.png /tmp/b/ && time homr /tmp/b'

  [!] 而且目錄模式**會把每張圖處理兩遍**（實測每張圖 10 次 tromr 推論而不是 5 次），
  所以要除以 2 再比。在開發機上原生量到的結果：

      3 個行程各 1 張   72 秒 ÷ 3 趟 = 24.0 秒/趟
      1 個行程 3 張×2   104 秒 ÷ 6 趟 = 17.3 秒/趟   →  差 6.7 秒/頁，約 28%

  28% 落在「值得考慮改成 import 內部 API 並在 lifespan 預熱」的門檻上，
  代價是綁一個沒有穩定性承諾的內部函式（見 app/engine.py 的 TODO(verify)）。
  上面那組數字 n=1，換到容器裡要重量一次再決定。""")

# 主控台編碼不支援的字元不該讓 bench 以非零結束（Windows 的 cp950 就是這樣）
try:
    print(note)
except UnicodeEncodeError:
    import sys
    print(note.encode(sys.stdout.encoding or "ascii", "replace")
              .decode(sys.stdout.encoding or "ascii"))
PY
