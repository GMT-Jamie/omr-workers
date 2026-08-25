#!/usr/bin/env bash
# 對跑起來的 worker 打 /health 與 /transcribe，驗收 spec §9-2 與 §9-3。
#
# 樣本圖**不在版控裡**（見 .gitignore：測試用的樂譜幾乎都有版權，而這是公開 repo），
# 所以路徑由參數給：
#
#   scripts/smoke_test.sh /path/to/page.png
#   BASE=http://127.0.0.1:8081 scripts/smoke_test.sh ~/scores/page.png
set -uo pipefail

BASE="${BASE:-http://127.0.0.1:8081}"
IMG="${1:-}"
TMP="${TMPDIR:-/tmp}"
pass=0; fail=0

# ─── 找一個真的能跑的 Python ───
#
# **不能直接用 `python3`。** Windows 的 Git Bash 上那是 Microsoft Store 的空殼：
# `command -v` 找得到、跑起來不輸出任何東西，於是所有 JSON 欄位都取到空字串，
# 而測試看起來像 worker 壞了（HTTP 狀態碼全對，只有欄位是空的）。
PYBIN=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && [ "$("$c" -c 'print(1)' 2>/dev/null)" = 1 ]; then
    PYBIN="$c"; break
  fi
done
[ -n "$PYBIN" ] || { echo "找不到可用的 python（試過 python3 / python / py）"; exit 2; }

ok()    { printf '  \033[32m✓\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()   { printf '  \033[31m✗\033[0m %s\n' "$1"; fail=$((fail+1)); }
head1() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# ─── HTTP ───
#
# `req` 設兩個全域變數而**不是**印出狀態碼。曾經是 `code=$(req …)` —— 那讓 req 跑在
# 子 shell 裡，於是它設的 BODY 出了子 shell 就消失。
#
# body 留在 shell 變數裡、用 stdin 餵給 python，**不經過檔案路徑**：Windows 的
# Git Bash 上 shell 的 `/tmp` 與 Windows Python 的 `/tmp` 是兩個不同的目錄。
BODY=""
CODE=""
req() {
  local out
  out=$(curl -sS -w '\n%{http_code}' "$@")
  CODE="${out##*$'\n'}"
  BODY="${out%$'\n'*}"
}

# 從 BODY 取值。**每個 python 片段都是單行的 -c** —— 在 shell 裡塞多行 python 的話
# 巢狀引號會安靜地讓整段不執行，而狀態碼照樣是對的，所以那種壞法很難發現。
pyget() { printf '%s' "$BODY" | "$PYBIN" -c "$1" 2>/dev/null; }
field() { pyget "import json,sys;print(json.load(sys.stdin).get('$1',''))"; }
deep()  { pyget "import json,sys;print(json.load(sys.stdin).get('$1',{}).get('$2',''))"; }
nlist() { pyget "import json,sys;print(len(json.load(sys.stdin).get('$1',[])))"; }
warn1() { pyget "import json,sys;v=json.load(sys.stdin).get('warnings',[]);print(v[0] if v else '')"; }
mxlen() { pyget "import json,sys;print(len(json.load(sys.stdin).get('musicxml','')))"; }

head1 "GET /health"
req "$BASE/health"
[ "$CODE" = 200 ] && ok "200" || bad "預期 200，得到 $CODE"
[ "$(field ready)" = True ] && ok "ready: true" || bad "ready 不是 true（權重沒烘進 image？）"
printf '    engine=%s version=%s\n' "$(field engine)" "$(field engine_version)"

head1 "POST /transcribe：內容不是圖片應該回 400 INVALID_INPUT（spec §9-3）"
# 副檔名對、內容不對 —— worker 檢查魔術位元組，所以這裡就該被擋，
# 而不是讓 homr 花 20 秒才失敗（那 20 秒佔著唯一的工作位）
printf 'this is definitely not a png' > "$TMP/smoke_fake.png"
req -F "file=@$TMP/smoke_fake.png" "$BASE/transcribe"
[ "$CODE" = 400 ] && ok "400" || bad "預期 400，得到 $CODE"
[ "$(field error_code)" = INVALID_INPUT ] && ok "INVALID_INPUT" || bad "error_code=$(field error_code)"

head1 "POST /transcribe：不支援的副檔名也要 400"
printf 'x' > "$TMP/smoke.txt"
req -F "file=@$TMP/smoke.txt" "$BASE/transcribe"
[ "$CODE" = 400 ] && ok "400" || bad "預期 400，得到 $CODE"
[ "$(field error_code)" = INVALID_INPUT ] && ok "INVALID_INPUT" || bad "error_code=$(field error_code)"

head1 "POST /transcribe：合法的圖但上面沒有五線譜 → 422，而且訊息要是人話"
# ⚠️ **這一項守的是「訊息長什麼樣」，不是狀態碼。**
#
# 使用者傳錯檔案是最常見的失敗，而在加這道守衛之前他看到的是一段 Java stack trace
# 加一個容器內的暫存路徑（`/tmp/audiveris-xxxx/out/page.omr`）—— 因為失敗訊息取的是
# 引擎輸出的**最後** 2000 字，而 Audiveris 真正的診斷在**最上面**。
#
# ⚠️ 那種壞法**光看狀態碼看不出來**：HTTP 422 是對的、error_code 是對的，只有
# message 是垃圾。所以下面除了狀態碼，還要斷言訊息裡沒有引擎內部細節。
#
# 全白的圖就夠觸發（實測整頁文字、照片雜訊、全白三種走的都是同一條路徑）。
# 就地生成，不進版控 —— 這個 repo 不放樣本圖。
"$PYBIN" -c "import zlib,struct,sys;w,h=1200,1600;raw=b''.join(b'\x00'+b'\xff'*w for _ in range(h));ck=lambda t,d:struct.pack('>I',len(d))+t+d+struct.pack('>I',zlib.crc32(t+d)&0xffffffff);open(sys.argv[1],'wb').write(b'\x89PNG\r\n\x1a\n'+ck(b'IHDR',struct.pack('>IIBBBBB',w,h,8,0,0,0,0))+ck(b'IDAT',zlib.compress(raw,9))+ck(b'IEND',b''))" "$TMP/smoke_blank.png"
req -F "file=@$TMP/smoke_blank.png" "$BASE/transcribe"
[ "$CODE" = 422 ] && ok "422" || bad "預期 422，得到 $CODE"
[ "$(field error_code)" = RECOGNITION_FAILED ] && ok "RECOGNITION_FAILED" || bad "error_code=$(field error_code)"
msg=$(field message)
case "$msg" in
  *org.audiveris*|*.java:*|*/tmp/*)
    bad "訊息裡有引擎內部細節：$(printf '%.90s' "$msg")…" ;;
  "")
    bad "沒有 message" ;;
  *)
    if [ "${#msg}" -lt 200 ]; then
      ok "訊息是人話（${#msg} 字）：$msg"
    else
      bad "訊息 ${#msg} 字，像是整段原始引擎輸出"
    fi ;;
esac

if [ -z "$IMG" ]; then
  head1 "略過辨識與併發測試"
  echo "    沒給樣本圖。用法：scripts/smoke_test.sh /path/to/page.png"
else
  [ -f "$IMG" ] || { echo "找不到 $IMG"; exit 2; }

  head1 "POST /transcribe：真的圖片應該回 200 且 musicxml 非空（spec §9-2）"
  t0=$(date +%s)
  req -F "file=@$IMG" -F 'options={"tempo_bpm":120}' "$BASE/transcribe"
  el=$(( $(date +%s) - t0 ))
  if [ "$CODE" = 200 ]; then
    ok "200（${el} 秒）"
  else
    bad "預期 200，得到 $CODE"
    printf '    %s\n' "$BODY"
  fi
  n=$(mxlen); n=${n:-0}
  if [ "${n:-0}" -gt 100 ] 2>/dev/null; then ok "musicxml $n 字元"; else bad "musicxml 太短或空的（$n）"; fi
  printf '    timing_ms: total=%s inference=%s\n' "$(deep timing_ms total)" "$(deep timing_ms inference)"

  # `--output-tempo` **單獨給是 no-op** —— homr 0.7.0 的 build_add_time_direction()
  # 開頭就是 `if not args.metronome: return None`，所以 worker 必須同時傳
  # `--output-metronome`。這一項就是在守那件事：它壞掉的話 MusicXML 沒有速度，
  # 下游只會用預設速度播，而**沒有任何錯誤訊息**。
  case "$BODY" in
    *'<sound tempo='*) ok "MusicXML 裡有 <sound tempo>（tempo_bpm 真的傳到了）" ;;
    *) bad "沒有 <sound tempo> —— worker 沒有同時傳 --output-metronome？" ;;
  esac

  # 引擎自己丟掉的東西有沒有被撈出來。homr 把那些訊息寫在 **stderr**，
  # 而 MusicXML 上沒有任何痕跡 —— 這個欄位是唯一說得出那件事的地方。
  nw=$(nlist warnings); nw=${nw:-0}
  printf '    warnings: %s 條\n' "$nw"
  if [ "${nw:-0}" -gt 0 ] 2>/dev/null; then printf '      %s\n' "$(warn1)"; fi

  head1 "併發第二個請求應該立即回 429 BUSY（spec §9-3）"
  curl -sS -o /dev/null -F "file=@$IMG" "$BASE/transcribe" &
  bgpid=$!
  sleep 3                                   # 讓第一個請求真的進到 semaphore 裡
  req -F "file=@$IMG" "$BASE/transcribe"
  [ "$CODE" = 429 ] && ok "429" || bad "預期 429，得到 $CODE"
  [ "$(field error_code)" = BUSY ] && ok "BUSY" || bad "error_code=$(field error_code)"
  wait $bgpid 2>/dev/null || true
fi

head1 "結果"
printf '  %d 通過 / %d 失敗\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
