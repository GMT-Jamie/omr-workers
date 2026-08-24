# omr-workers

五線譜圖片 → MusicXML 的容器化 OMR worker。給 [mml.mabi.tw](https://mml.mabi.tw)
(MML 工房) 用,但介面裡沒有任何它專屬的東西。

**這些 worker 是啞巴工人**:收圖、回 MusicXML。排隊、配額、認證、路由、
MusicXML→MML 轉換全部歸呼叫端。

| worker | 引擎 | 上游 | 狀態 |
|---|---|---|---|
| `homr-worker` | [homr](https://github.com/liebharc/homr) 0.7.0 | AGPL-3.0 | 骨架完成,**尚未在容器裡驗證** |
| `audiveris-worker` | [Audiveris](https://github.com/Audiveris/audiveris) | AGPL-3.0 | 還沒開始 |

## 授權

**AGPL-3.0**(全文見 [LICENSE](LICENSE))。`homr-worker` 的 wrapper `import homr`,
構成衍生作品,所以整個 repo 以 AGPL-3.0 授權並公開發佈。上游來源見上表。

呼叫端(MML 工房的 ASP.NET 站台)走 HTTP 跟這裡講話 —— 獨立行程、只交換 MusicXML
字串、不共用資料結構 —— 所以它不是衍生作品,授權不同。**這是這兩個 repo 分開的
主要原因**,請不要把它們合併,也不要把任何 3ml 的程式碼複製進來。

---

## 跑起來

需要能跑 **Linux 容器**的 Docker。

```bash
# 開發(會把 port 映射到 host 的 127.0.0.1:8081)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build

# 正式(不對外開任何 port)
docker compose up --build -d
```

`build` 需要外網 —— `homr --init` 會從 GitHub releases 抓 157 MB 的模型權重。
**跑起來之後不需要網路**,權重已經烘進 image。

### 測試

樣本圖與 OMR 輸出**都不在版控裡**。理由:拿來測的樂譜幾乎都有版權(實測用的那份
頁腳就寫著「未經編者及原作者同意,請勿轉載」),而這是公開 repo。所以腳本吃路徑參數,
樣本自己放:

```bash
scripts/smoke_test.sh /path/to/page.png     # /health + /transcribe + 400 + 429
scripts/bench.sh      /path/to/page.png 10  # 推論時間分位數
```

`bench.sh` 的數字**不是附錄**,見下面〈怎麼讀 bench 的數字〉。

---

## HTTP 介面

### `GET /health`

```json
{ "status": "ok", "engine": "homr", "engine_version": "0.7.0", "ready": true }
```

`ready` 的語意是 **「引擎可執行 + 權重在 image 裡」**,不是「模型已載入記憶體」。
homr-worker 走 subprocess,每個請求是一個新行程,所以沒有常駐的模型可以反映。
原始工作說明書要求預載常駐,那件事延到 bench 出數字之後再決定 —— 見下面。

### `POST /transcribe`

`multipart/form-data`,欄位 `file`(必填)與 `options`(選填 JSON 字串)。

- **homr-worker 只吃 `png` / `jpg` / `jpeg`**。PDF 是 audiveris-worker 的活
  (homr 的 PDF 支援在 0.7.0 之後才進 main,還沒發版)。
- 上限 20 MB(`MAX_UPLOAD_MB`),邊讀邊算,超過立刻 413。
- 副檔名**與**魔術位元組都檢查。一個改名成 `.png` 的檔案否則會佔著唯一的工作位
  跑 20 秒才失敗。

`options`:

```json
{ "tempo_bpm": 120 }
```

**只有這一個參數,而它不是可選的裝飾。** 兩件事疊在一起:

1. homr 的 `--output-tempo` 是「把你給的 BPM 寫進 MusicXML」,**不是辨識譜上印的
   速度記號** —— 實測三頁的譜 `<sound tempo>` 數量是 0,譜面上印的 ♩=179 拿不到。
2. **而 `--output-tempo` 單獨給還是 no-op。** homr 0.7.0 的
   `build_add_time_direction()` 第一行就是 `if not args.metronome: return None`,
   整個 `<direction>` 區塊(`<sound tempo>` 也在裡面)只在給了 `--output-metronome`
   時才產生。所以 worker **兩個旗標都傳**,同一個值。

不傳 `tempo_bpm` 的話產出的 MusicXML 沒有速度,下游只能用預設速度播 ——
而那件事**沒有任何錯誤訊息**,所以 `smoke_test.sh` 有一項專門守它。

成功回 `200`:

```json
{
  "musicxml": "<?xml version=…",
  "engine": "homr",
  "engine_version": "0.7.0",
  "timing_ms": { "total": 24100, "inference": 23800 },
  "warnings": ["引擎刪掉了 10 個小節裡的連音(第 3、7、9… 小節)"]
}
```

**`warnings` 不是裝飾。** homr 會宣布 `Removing tuplets from measure # N` 然後把那些
連音直接刪掉,而 **MusicXML 上沒有任何痕跡**。實測那份三頁的譜第 3 頁 22 個小節裡有
10~11 個被動過。不撈出來的話那件事對呼叫端與使用者都完全不可見。

⚠️ 那些訊息在 **stderr,不是 stdout**(homr 用一個 eprint 助手,連
`Result was written to …` 都走 stderr)。只掃 stdout 的話 `warnings` 會永遠是空陣列,
而**那個壞法完全沒有症狀**。

錯誤統一 `{"error_code": "…", "message": "…"}`:

| HTTP | `error_code` | 情境 |
|---|---|---|
| 400 | `INVALID_INPUT` | 副檔名不支援、魔術位元組不對、空檔、`options` 解析失敗 |
| 413 | `FILE_TOO_LARGE` | 超過 `MAX_UPLOAD_MB` |
| 422 | `RECOGNITION_FAILED` | 引擎跑完但沒產出有效 MusicXML(**不嘗試修補半壞的 XML**) |
| 429 | `BUSY` | 已在處理另一個請求 |
| 500 | `INTERNAL_ERROR` | 未預期例外(細節只進日誌,不進回應) |
| 504 | `TIMEOUT` | 超過 `JOB_TIMEOUT_SEC` |

### 單工是刻意的

容量 1 的 semaphore(`MAX_CONCURRENCY`),滿載**立即回 429,不排隊**。單頁推論是
20 秒級的 CPU 佔用,排隊只是把「現在很忙」延後成「等了兩分鐘還是很忙」;而呼叫端
本來就是決定排隊策略的那一層,它看得到 429 才能決定要不要重試。

`uvicorn --workers 1` 是配套的:多開 worker 會讓 semaphore 只管到自己的行程,
於是 429 失效而兩個推論一起搶 2 顆 CPU。

### 環境變數

| 變數 | 預設 | |
|---|---|---|
| `JOB_TIMEOUT_SEC` | 600 | 單 job 時限,超過回 504 |
| `MAX_CONCURRENCY` | 1 | semaphore 容量 |
| `MAX_UPLOAD_MB` | 20 | 上傳上限 |
| `PORT` | 8080 | |

---

## 部署拓樸

```
瀏覽器 ──HTTPS──> IIS / Windows Server 2019(3ml 站台,薄代理)
                     │
                     │ Hyper-V Internal Switch(只通 host ↔ VM,VM 連不到外網)
                     ▼
                  Linux VM ── docker compose
                     ├── audiveris-worker  ← 主引擎
                     └── homr-worker       ← 備選,profiles: ["fallback"],預設不啟動
```

**Windows Server 2019 跑不了 Linux 容器**,這不是設定問題:WSL2 的 Server 支援從
Server 2022 才開始(2019 只有 WSL1,跑不了 Docker)、Docker Desktop 不支援任何
Windows Server、LCOW 早已移除。Server 2019 內建的 Docker 只能跑 Windows 容器。
所以容器住在同一台機器上的 Hyper-V Linux VM 裡。

用 **Internal Switch** 而不是 External/NAT:那種 switch 只通 host ↔ VM,於是
「runtime 無 outbound 網路」是 hypervisor 層級的保證,比 compose 設定更強。
build 需要外網,所以流程是 **開 External switch → build → 換回 Internal switch → 跑**。

⚠️ **compose 那一層刻意不設 `internal: true`**,而那不是放鬆。`internal: true` 的
網路上**不能發佈 port,而且 docker 不報錯** —— 它照收 `ports:`、`docker inspect`
顯示 `Ports=map[8080/tcp:[]]`(空的),然後主機怎麼打都不通。實測過。而主機正是
C# 代理所在的地方,所以那個設定會讓整條路不通。隔離交給上面那個 Internal switch。

瀏覽器**不能**直接打 worker:頁面是 HTTPS 而 worker 是私有 IP 上的純 HTTP,
那是 mixed content,瀏覽器直接封鎖。中間那層 C# 代理同時是認證與配額的閘門。

### 容器加固

non-root(uid 10001)、`read_only: true`、只有 `/tmp` 是 tmpfs、`cap_drop: ALL`、
`no-new-privileges`。`HOME` / `XDG_CACHE_HOME` / `TMPDIR` 全部指到 `/tmp` ——
任何往家目錄寫快取的相依否則會在 runtime 才炸,而錯誤訊息指不到 rootfs 唯讀這件事。

---

## 怎麼讀 bench 的數字

`bench.sh` 給 `total` 與 `inference` 的 min / p50 / p90 / max,以及兩者的差。

**p50 的 `total` 決定呼叫端的形狀。** 單頁 < ~20 秒,C# 那層同步撐著最簡單;
> 30 秒就得改前端輪詢或 SSE,而且要處理「使用者以為當掉了按重新整理 → 第二個
請求 → 429 BUSY」。它同時是配額與 `JOB_TIMEOUT_SEC` 的依據。

**`total - inference` 的差不是 session 冷啟動。** 實測只有 **10~13 ms**(佔 0.1%)
—— 那是 FastAPI 的請求開銷,可以忽略。原因是 `inference` 量的是**整個 subprocess**
的執行時間,而 ONNX session 的建立就發生在它裡面。

**要量冷啟動得比較「一個行程處理 N 張」與「N 個行程各 1 張」。** homr 吃目錄,
但**目錄模式會把每張圖處理兩遍**(實測每張圖 10 次 tromr 推論而不是 5 次),
所以要除以 2 才是同一個工作量:

| | 每趟 |
|---|---|
| 3 個行程各 1 張 | 72 秒 ÷ 3 = **24.0 秒** |
| 1 個行程 3 張×2 | 104 秒 ÷ 6 = **17.3 秒** |

差 **6.7 秒/頁,約 28%** —— 行程啟動 + session 建立的代價。28% 落在「值得考慮改成
`import homr.main.process_image` 並在 lifespan 預熱」的門檻上,而不是「明顯不值得」。
代價是綁一個**沒有穩定性承諾的內部 API**。上面那組數字 n=1 且是原生環境,
換到容器裡要重量一次再決定(見 `app/engine.py` 的 `TODO(verify)`)。

參考值(Windows 開發機、**原生不經容器**、homr 0.7.0、一頁乾淨的 A4 鋼琴譜、
5 個譜表系統、21 小節):單張 **19~24 秒**,其中 transformer 推論 15.4 秒。
經過 FastAPI 的 p50 是 **19.2 秒**。**容器裡的數字還沒量。**

---

## 已知的事實與坑

全部是實測出來的,不是從文件抄的。

- **homr 0.7.0 跑 onnxruntime,沒有 torch。** 相依是 numpy / opencv-headless /
  Pillow / rapidocr / onnxruntime / requests / musicxml。權重 157 MB
  (segnet 57 + encoder 53 + decoder 47),rapidocr 的 OCR 權重 32 MB 直接內建在 wheel 裡。
- **`homr --init` 就是烘權重的那一步**,說明字面寫著「if you want to prepare for
  example a Docker image」。它也是唯一會一併抓 OCR 權重的路徑。不需要放樣本圖跑
  一次推論(於是有版權問題的樂譜不必進 image)。
- **權重落在 site-packages 內部**(`homr/transformer/*.onnx`、`homr/segmentation/*.onnx`)。
  runtime 是唯讀 rootfs,所以 Dockerfile **明確驗證那些檔案存在** —— `--init` 沒生效
  的話 runtime 會試著往唯讀路徑下載然後失敗,而錯誤訊息指不到真正的成因。
- **輸出寫在輸入檔旁邊**(`<input>.musicxml`),另外還吐一個 `<input>_teaser.png`
  (辨識結果的視覺化,對除錯很有用)。所以輸入必須先複製到自己的暫存目錄。
- **`musicxml` 套件用 `ET.parse()` 讀 XSD 沒有指定編碼**,走 locale 預設。非 UTF-8
  locale 上直接炸 `UnicodeDecodeError`(在繁中 Windows 的 cp950 上實測到)。
  Dockerfile 與 subprocess 環境都設了 `PYTHONUTF8=1`。
- **`opencv-python` 與 `opencv-python-headless` 會同時被裝進來**(homr 要 headless,
  它相依的 rapidocr 要非 headless),兩者裝到同一個 `cv2` 路徑。所以 image 裡裝了
  `libgl1` 與 `libglib2.0-0`。
- **0.7.0 沒有 `--no-title`**(那是 main 分支才有的),標題 OCR 關不掉。它也不太可靠:
  實測三頁分別認出編曲者名、空字串、正確標題。
- **0.7.0 沒有 PDF 支援**(`pypdfium2` 在 main 的 pyproject 裡,不在 0.7.0 的相依裡)。
- **辨識品質的實測**:一頁乾淨的數位排版鋼琴譜,譜表系統 5/5、小節數 21/21、譜號、
  調號、拍號、大譜表分組全部正確。但**66 個小節裡有 28 個(42%)的音符時值加起來
  不等於一整個小節**,而且每一個都偏長 —— 引擎把時值讀大了。這件事在 MusicXML 上
  沒有標記,由呼叫端自己算。

## 還沒做

**逐項的待辦、卡住的決定、與踩過的坑都在 [TODO.md](TODO.md)** —— 接手前先讀那一份。
下面是摘要。

- `audiveris-worker`。乾淨印刷譜與掃描 PDF 是它的主場(而 homr 是為手機拍攝最佳化的),
  而且 Audiveris 有完整的符號辨識管線,`<sound tempo>` 是它真的會偵測的東西。
- **容器裡從來沒跑過。** `Dockerfile` 與 compose 都還沒 build 過,所以斷網驗證、
  non-root、唯讀 rootfs 這幾項(spec §9-4、§9-6)都還沒驗。

  但**應用層已經在原生環境完整驗過**:`scripts/smoke_test.sh` 對著
  `uvicorn app.main:app` 跑出 11/11 —— `/health` 的 `ready`、`400 INVALID_INPUT`
  (魔術位元組與副檔名兩種)、`200` 加非空 MusicXML、`<sound tempo>` 真的有寫進去、
  `warnings` 真的撈到了、以及併發第二個請求的 `429 BUSY`。`bench.sh` 也跑得出分位數。
- requirements 只 pin 了頂層四個套件。要完全可重現的話下一步是 `pip-compile`
  出一份 hash 齊全的 lock 檔。
- `MAX_CONCURRENCY > 1` 沒有意義(uvicorn 只開一個 worker,而單頁就吃滿 2 顆 CPU),
  留著只是為了不寫死。
