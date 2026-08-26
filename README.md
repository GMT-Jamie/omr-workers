# omr-workers

樂譜圖片（或 PDF）→ MusicXML 的容器化 OMR worker。兩個引擎、同一套 HTTP 契約。

```
POST /transcribe   (multipart: file=page.png)
  → 200 { "musicxml": "<?xml …", "timing_ms": {...}, "warnings": [...] }
```

---

## 這是什麼、用在哪

**OMR**（Optical Music Recognition，光學樂譜辨識）把五線譜的圖轉成 MusicXML。
這個 repo 把兩套開源 OMR 引擎各自包成一個**沒有狀態、單工、不連外網**的 HTTP 服務，
讓任何後端可以用一個 multipart POST 使用它們，而不必處理 JVM、ONNX、模型權重、
OCR 語言資料這些事。

作者拿它給 [mml.mabi.tw](https://mml.mabi.tw)（MML 工房）用 —— 使用者上傳樂譜圖片，
站台轉成《瑪奇》的 MML 樂譜。但**介面裡沒有任何它專屬的東西**，任何需要
「圖 → MusicXML」的專案都可以直接拿去用。

**這些 worker 是啞巴工人**：收圖、回 MusicXML。排隊、配額、認證、路由、多頁合併、
MusicXML 的後處理**全部歸呼叫端**。這條界線是刻意的 —— 它讓 worker 可以被替換、
被獨立測試，也讓授權邊界清楚（見下）。

適合的情況：

- 你有一個後端，想加「上傳樂譜 → 拿到 MusicXML」的功能，但不想把 OMR 引擎的相依
  塞進自己的部署。
- 你想在**沒有 outbound 網路**的機器上跑 OMR（模型權重與 OCR 資料全部烘在 image 裡）。
- 你想比較兩個引擎在自己的譜上的表現 —— 兩者的對外契約完全相同，換一個容器就好。

不適合的情況：這裡沒有排隊系統、沒有認證、沒有多頁樂譜的組裝，也不做 MusicXML→MIDI /
ABC / MML 的轉換。滿載時它直接回 `429`，由你決定要不要重試。

## 兩個 worker

| worker | 引擎 | 輸入 | 單頁 | image | 上游授權 |
|---|---|---|---|---|---|
| **`audiveris-worker`**（預設） | [Audiveris](https://github.com/Audiveris/audiveris) 5.11.0 | `png` `jpg` **`pdf`** | **4～8 秒** | 805 MB | AGPL-3.0 |
| `homr-worker`（備選） | [homr](https://github.com/liebharc/homr) 0.7.0 | `png` `jpg` | 13～96 秒 | 1.51 GB | AGPL-3.0 |

同一批三頁鋼琴譜（66 小節、乾淨數位排版）的實測對照：

| | homr | audiveris |
|---|---|---|
| 時值湊不滿一小節的小節 | 28/66（42%） | **19/66（29%）** |
| 連音 | **0** —— 引擎自己宣布刪掉了 11 個小節的 | **5 個保留** |
| 譜上印的速度記號 | 讀不到 | **♩=179 讀到了** |
| 連結線 | 全標 `<slur>`，下游得靠「音高相同」猜 | **`<tied>` 直接給** |
| 單頁（4 核） | 95.9 秒 | **4.1 秒** |
| 單頁（24 核） | 13.9 秒 | 4.6 秒 |
| 對核心數的敏感度 | **極大**（2 核 200 秒 → 24 核 13 秒） | 幾乎沒有（2 核 5.2 秒） |

**所以預設是 audiveris**，homr 以 compose profile 保留。homr 的宣稱強項是**手機拍攝**
的譜，而上面這批全是乾淨的數位排版 —— 那正好是 audiveris 的主場。手邊沒有翻拍樣本，
所以那一半沒有結論，兩個都留著。

---

## 授權

**AGPL-3.0**（全文見 [LICENSE](LICENSE)）。

兩個引擎都是 AGPL-3.0，而 `homr-worker` 的 wrapper 直接 `import homr`、
`audiveris-worker` 隨 image 散布 Audiveris —— 所以整個 repo 以 AGPL-3.0 授權並公開發佈。

如果你把這些 worker 架起來對外提供服務，**AGPL §13 的義務會落在你身上**：使用者
（包含只是透過你的網站間接用到它的人）有權取得對應的完整原始碼，包括你自己改過的部分。
那個義務在**你開始提供服務的那一刻**觸發。

### 我們改過 Audiveris（AGPL §5(a)）

`audiveris-worker` 的 image 裡跑的 Audiveris **不是原封不動的 5.11.0**。修改一共
一個檔案、80 行 diff，就放在
[`audiveris-worker/patches/single-bar-repeat.patch`](audiveris-worker/patches/single-bar-repeat.patch)，
build 的時候套上去（見 Dockerfile 裡的〈補丁〉那一段，也在那裡說明了原因）。

修的是**起始反覆 `|:` 認不出來**：粗線與細線印在一起、中間沒有白邊的譜（實測是
一根 15 px 寬的線，而不是 4+1+11 三段），Audiveris 會因為「只有一根小節線」而
放棄，即使它已經正確辨識出旁邊那對反覆點。症狀是整段反覆只播一遍。補丁加的規則
是「一根小節線 + 那一側有一對反覆點 = 反覆記號」。

上游的 `.deb` 本身仍然是**原版**（pin + checksum 沒動），補丁是編成一個 class 蓋在
classpath 前面的。

### 呼叫端不會因此被感染

呼叫端（作者這邊是一個 ASP.NET 站台）走 HTTP 跟這裡講話 —— **獨立行程、只交換
MusicXML 字串、不共用任何資料結構或函式庫**。它因此不是衍生作品，可以是任何授權。

這是那個站台與這個 repo **刻意分成兩個 repo** 的主要原因。如果你 fork 這個專案，
請維持同樣的界線：不要把呼叫端的程式碼複製進來，也不要讓 worker 反過來 import
呼叫端的東西。一旦跨過去，兩邊就都變成 AGPL 的衍生作品了。

---

## 跑起來

需要能跑 **Linux 容器**的 Docker（Docker Engine 或 Docker Desktop 都可以）。

```bash
# 開發：把 port 映到 host 的 127.0.0.1:8081
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build

# 正式：不對外開任何 port（見〈部署拓樸〉）
docker compose up -d --build

# 改用備選引擎 homr
docker compose --profile fallback up -d --build homr-worker
```

`build` 需要外網：audiveris 要抓 `.deb` 與 OCR 語言資料，homr 的 `homr --init` 要從
GitHub releases 抓 157 MB 的模型權重（兩者都對 checksum）。
**build 完之後 runtime 完全不需要網路** —— 已實測在斷網的容器裡辨識仍然正常，
連譜上印的速度記號都還讀得到。

試一下：

```bash
curl http://127.0.0.1:8081/health
curl -F 'file=@page.png' -F 'options={"tempo_bpm":120}' \
     http://127.0.0.1:8081/transcribe
```

### 測試腳本

```bash
scripts/smoke_test.sh /path/to/page.png     # /health + /transcribe + 400/413/429
scripts/bench.sh      /path/to/page.png 10  # 推論時間分位數
BASE=http://192.168.1.5:8081 scripts/smoke_test.sh page.png   # 打別台
```

樣本圖與 OMR 輸出**都不在版控裡**，所以腳本吃路徑參數。理由：拿來測的樂譜幾乎都有
版權（實測用的那份頁腳就寫著「未經編者及原作者同意，請勿轉載」），而這是公開 repo；
產出的 `.musicxml` 是它的衍生資料，同樣不行。`.gitignore` 釘住了這件事。

---

## HTTP 介面

兩個 worker 的介面**完全相同**，呼叫端不需要知道背後是哪個引擎。

（程式碼註解裡的 `spec §N` 指的是呼叫端專案裡的一份內部契約文件。它規定的東西
就是下面這幾節，這裡是唯一的公開版本。）

### `GET /health`

```json
{ "status": "ok", "engine": "audiveris", "engine_version": "5.11.0", "ready": true }
```

`ready` 的語意是 **「引擎可執行 + 模型／OCR 資料在 image 裡」**，不是「模型已載入
記憶體」。兩個 worker 都走 subprocess，每個請求是一個新行程（audiveris 是新的 JVM），
所以沒有常駐的模型可以反映。這是量過之後的決定，不是省略：一個行程處理多張圖只快
約 28%（homr）／幾乎沒差（audiveris），而代價是綁一個沒有穩定性承諾的內部 API。

`ready: false` 時服務仍然會起來，並在 stdout 講清楚缺的是哪一種東西 —— 因為兩種缺失
的修法完全不同。

### `POST /transcribe`

`multipart/form-data`，欄位 `file`（必填）與 `options`（選填，JSON 字串）。

- **audiveris-worker 吃 `png` / `jpg` / `jpeg` / `pdf`**；homr-worker 只吃前三種
  （homr 的 PDF 支援在 0.7.0 之後才進 main，還沒發版）。
- 上限 20 MB（`MAX_UPLOAD_MB`），**邊讀邊算**，超過立刻 413，不會先把整個上傳吞進記憶體。
- 副檔名**與**魔術位元組都檢查。一個改名成 `.png` 的檔案否則會佔著唯一的工作位
  跑好幾秒才失敗。
- **一個檔案 = 一個請求**，但「一個檔案」可以是**整份多頁 PDF** ——
  audiveris-worker 一次看完整本，回**一份**連續的 MusicXML。實測一份七頁的譜
  48.8 秒、只吐一個 `.mxl`。
- **有 PDF 就送 PDF，不要先拆成圖片。** 拆頁會製造下面〈多頁的兩個陷阱〉那兩個問題
  （速度、拍號），而整本送進去這兩個都不存在。拆頁只在手上真的只有圖片時才需要。
  ⚠️ 但那一份 MusicXML 裡 `<divisions>` 是**一頁一個而且值不一樣**（實測七頁是
  `4, 96, 4, 4, 4, 4, 2`），呼叫端的換算必須逐個時值做 —— 拿「這份文件的
  divisions」去除會讓整首曲子長好幾倍，而症狀只有一句不起眼的警告。

`options` 目前只有一個參數：

```json
{ "tempo_bpm": 120 }
```

**兩個引擎對它的態度不同，但保證的結果一樣**（輸出裡有 `<sound tempo>`）：

| | audiveris | homr |
|---|---|---|
| 譜上印的速度記號 | **讀得到**，讀到就用它，`tempo_bpm` 是退路 | 讀不到，`tempo_bpm` 是唯一來源 |
| 讀到的值跟你給的不同 | 照譜上的播，並在 `warnings` 說一句 | 不適用 |
| 不傳 `tempo_bpm` | 譜上沒印就沒有速度 | **沒有速度**，下游只能用預設值播 |

「沒有速度」這件事**不會有任何錯誤訊息**，所以 `smoke_test.sh` 有一項專門守它。

成功回 `200`：

```json
{
  "musicxml": "<?xml version=…",
  "engine": "audiveris",
  "engine_version": "5.11.0",
  "timing_ms": { "total": 8300, "inference": 8100 },
  "warnings": ["譜上印的速度是 ♩=179，已經照它播（你填的 120 沒有用到）"]
}
```

### `warnings` 不是裝飾

它撈的是**引擎自己丟掉了什麼、而 MusicXML 上沒有痕跡的那些事**。不看它的話，那些
問題對呼叫端與使用者都完全不可見：

- **homr**：`Removing tuplets from measure # N` —— 它會把連音直接刪掉。實測那份三頁
  的譜，第 3 頁 22 個小節裡有 10～11 個被動過。
  ⚠️ 這些訊息在 **stderr，不是 stdout**（homr 連 `Result was written to …` 都走 stderr）。
- **audiveris**：某一頁沒有拍號時它檢查不了小節時值，會列出受影響的小節。
  ⚠️ 那句話的意思是**「我不知道一小節該多長」**，不是「這些小節壞了」—— 印刷譜通常
  只在第一頁印拍號，於是第 2、3 頁**每一個小節**都會被列出來。把它誤譯成「壞小節」
  會讓一份實際只有幾個問題的譜報成全毀。
- **速度**：譜上讀到的速度跟你傳的 `tempo_bpm` 不一致時。

### 錯誤

一律 `{"error_code": "…", "message": "…"}`：

| HTTP | `error_code` | 情境 |
|---|---|---|
| 400 | `INVALID_INPUT` | 副檔名不支援、魔術位元組不對、空檔、`options` 解析失敗 |
| 413 | `FILE_TOO_LARGE` | 超過 `MAX_UPLOAD_MB` |
| 422 | `RECOGNITION_FAILED` | 引擎跑完但沒產出有效 MusicXML（**不嘗試修補半壞的 XML**） |
| 429 | `BUSY` | 已在處理另一個請求 |
| 500 | `INTERNAL_ERROR` | 未預期例外（細節只進日誌，不進回應） |
| 504 | `TIMEOUT` | 超過 `JOB_TIMEOUT_SEC` |

`message` 是**給維運看的**，不是給終端使用者看的。呼叫端應該對 `error_code` 顯示
自己的（多語系）字串。唯一的例外是「這張圖裡找不到五線譜」—— 那是使用者最常踩的
失敗（傳錯檔、翻拍太糊、解析度太低），worker 直接給了一句人話。

### 單工是刻意的

容量 1 的 semaphore（`MAX_CONCURRENCY`），滿載**立即回 429，不排隊**。單頁推論整段
佔著 CPU，排隊只是把「現在很忙」延後成「等了兩分鐘還是很忙」；而呼叫端本來就是決定
排隊策略的那一層，它看得到 429 才能決定要不要重試、要不要告訴使用者。

`uvicorn --workers 1` 是配套的：多開 worker 會讓 semaphore 只管到自己的行程，
於是 429 失效，而兩個推論一起搶同一份 CPU 配額。

### 環境變數

| 變數 | 預設 | |
|---|---|---|
| `JOB_TIMEOUT_SEC` | 600 | 單 job 時限，超過回 504 |
| `MAX_CONCURRENCY` | 1 | semaphore 容量（見上，不建議調大） |
| `MAX_UPLOAD_MB` | 20 | 上傳上限 |
| `PORT` | 8080 | 容器內的 port |
| `OMR_BIND` | `127.0.0.1` | 正式 compose 把 8081 綁到哪個位址 |

日誌是 JSON Lines、走 stdout（rootfs 唯讀，寫進 tmpfs 的日誌重啟就消失，那不叫日誌）。
**不記錄圖片內容，也不記檔名** —— 使用者的檔名可能就是曲名。只記大小、耗時、狀態。

---

## 多頁的兩個陷阱

**這兩個只在「拆成圖片、一張一個請求」時存在。** 送整份 PDF 進 audiveris-worker
的話兩個都不會發生 —— 它一次看完整本，速度讀一次、拍號全程有效。這是選 PDF
那條路最實際的理由。

拆頁時兩個都是實際踩到才發現的，而且**修在呼叫端，worker 修不了**（它一次只看
一張圖，不知道自己是第幾頁）：

1. **`tempo_bpm` 只送第一頁。** 否則 audiveris 會在第 1 頁讀到譜上印的 ♩=179、
   第 2 頁沒印所以注入你給的 120 —— 產出的樂譜**從中間慢下來**。
2. **拍號通常只印在第一頁。** 要跨頁沿用，不然後面每一頁都會被判定「無法檢查小節」。

---

## 部署拓樸

作者這邊的實際部署，貼出來當一個可行的參考：

```
瀏覽器 ──HTTPS──> IIS / Windows Server 2019（站台，薄代理）
                     │
                     │ Hyper-V Internal Switch（只通 host ↔ VM，VM 連不到外網）
                     ▼
                  Linux VM ── docker compose
                     ├── audiveris-worker  ← 主引擎
                     └── homr-worker       ← 備選，profiles: ["fallback"]，預設不啟動
```

**瀏覽器不能直接打 worker**：頁面是 HTTPS 而 worker 是私有 IP 上的純 HTTP，那是
mixed content，瀏覽器直接封鎖。中間那層代理同時是認證與配額的閘門。

三個踩過的坑，如果你走類似的路：

- **Windows Server 2019 跑不了 Linux 容器**，這不是設定問題：WSL2 的 Server 支援從
  Server 2022 才開始、Docker Desktop 不支援任何 Windows Server、LCOW 早已移除。
  所以容器住在同一台機器上的 Hyper-V Linux VM 裡。
- **用 Internal Switch 而不是 External/NAT**：那種 switch 只通 host ↔ VM，於是
  「runtime 無 outbound 網路」是 hypervisor 層級的保證，比任何 compose 設定都強。
  build 需要外網，所以流程是**開 External → build → 換回 Internal → 跑**。
- ⚠️ **compose 那一層刻意不設 `internal: true`**，而那不是放鬆。`internal: true` 的
  網路上**不能發佈 port，而且 docker 不報錯** —— 它照收 `ports:`、`docker inspect`
  顯示 `Ports=map[8080/tcp:[]]`（空的），然後主機怎麼打都不通。實測過。而主機正是
  代理所在的地方。真的要在 docker 這層斷網的話，用 `docker-compose.offline.yml`。

### 容器加固

non-root（uid 10001）、`read_only: true`、只有 `/tmp` 是 tmpfs、`cap_drop: ALL`、
`no-new-privileges`。`HOME` / `XDG_CACHE_HOME` / `TMPDIR` 全部指到 `/tmp` ——
任何往家目錄寫快取的相依否則會在 runtime 才炸，而錯誤訊息指不到「rootfs 是唯讀的」
這件事。tmpfs 需要 `exec`：有些套件會把 `.so` 解到快取再 `dlopen`。

### 資源

audiveris 預設 `cpus: "2"` / `memory: 2g`，這是量過的：曲線是平的（2 核 5.2 秒、
4 核 4.1 秒、24 核 4.6 秒），給更多核心沒有用。**homr 相反** —— 它對核心數極度敏感
（2 核約 200 秒、4 核 106 秒、8 核 54 秒、16 核 24 秒、24 核 12～13 秒，輸出完全一致），
要用它請準備核心。

那**不是執行緒超額訂閱**：`--cpus=2`（`nproc` 仍看到 24）是 191 秒，改成
`--cpuset-cpus=0-1`（`nproc` 只看到 2）反而 218 秒。所以設 `OMP_NUM_THREADS`
之類的旗標沒有用，唯一的變數就是給幾顆核心。

---

## 怎麼讀 bench 的數字

`bench.sh` 給 `total` 與 `inference` 的 min / p50 / p90 / max，以及兩者的差。

**p50 的 `total` 決定呼叫端的形狀。** 單頁 < ~20 秒，同步撐著最簡單；> 30 秒就得改成
前端輪詢或 SSE，而且要處理「使用者以為當掉了按重新整理 → 第二個請求 → 429 BUSY」。
它同時是配額與 `JOB_TIMEOUT_SEC` 的依據。

**`total - inference` 的差不是模型冷啟動。** 實測只有 10～13 ms（佔 0.1%）—— 那是
FastAPI 的請求開銷。原因是 `inference` 量的是**整個 subprocess** 的執行時間，
而模型／JVM 的初始化就發生在它裡面。

參考值（正式機、2 vCPU、audiveris、一頁 A4 鋼琴譜）：**8.3 秒/頁**，三頁的譜約 25 秒。
同一份譜在 24 核開發機上是 5 秒 —— 差在單核效能，不在核心數。

---

## 已知的事實與坑

全部是實測出來的，不是從文件抄的。要改這個 repo 的話，這一節會省下你好幾天。

### 辨識品質（兩個引擎共通）

一頁乾淨的數位排版鋼琴譜：譜表系統 5/5、小節數 21/21、譜號、調號、拍號、大譜表分組
全部正確。但**66 個小節裡有 19～28 個的音符時值加起來不等於一整個小節**，而且幾乎
每一個都**偏長** —— 引擎把時值讀大了。

**這件事在 MusicXML 上沒有標記，由呼叫端自己算。** 如果你直接照檔案排時間軸，
三頁會累積約 5 個小節的漂移。作者這邊的解法是**硬對齊到節拍格**：小節起點用
「拍號算出來的累積位置」而不是「上一小節結束的游標」，超出的尾巴音符讓它重疊。
理由是節拍格是**已知的**而個別時值是**猜的**，而且「錯誤只影響一小節」使用者修得動，
「整首往後漂 5 個小節」修不動。拍號未知時必須退回忠實模式。

### Audiveris

- **輸出是 `.mxl`**（zip 過的 MusicXML），不是裸 XML。主檔名要照 zip 裡的
  `META-INF/container.xml` 讀，**不要猜**（猜錯的症狀是「辨識成功但回傳空字串」）。
  它同時還會吐一個 `.omr`（自己的 book 檔）與一份 `.log`。
- **不需要 Java base image** —— 官方 `.deb` 是 jpackage 打包的，自帶 JRE。
- **`.deb` 的 `Depends:` 沒有列 `libgtk-3-0t64`，但程式起不來。** 它在 `Main` 的
  **靜態初始化**就透過 JNA 載 gtk-3 做 HiDPI 縮放 —— 發生在解析參數之前，所以
  `-batch` 救不了。`apt install` 完全正常，第一次執行才炸 `UnsatisfiedLinkError`。
  Dockerfile 因此有一步 `Audiveris -version` 的驗證閘門，讓它在 **build** 就爆掉。
- **`postinst` 會失敗**：它呼叫 `xdg-desktop-menu install`，而容器裡沒有可寫的系統
  選單目錄 → `dpkg` 回錯誤碼 3、整個 build 失敗（檔案其實已經裝好）。
  先 `mkdir -p /usr/share/desktop-directories` 就沒事。
- 🔴 **OCR 語言資料必須是 [`tesseract-ocr/tessdata`](https://github.com/tesseract-ocr/tessdata)
  那個 repo 的版本** —— 不是 Ubuntu 的 `tesseract-ocr-eng` 套件，也不是 tessdata_fast /
  tessdata_best。**這是整個 image 裡最隱蔽的失敗**：那幾個都是 LSTM-only，而 Audiveris
  用 **legacy 引擎**，於是檔案在、啟動不報錯，只在 log 留一行
  `Could not initialize TessBaseAPI languages: eng in legacy mode`，然後**安靜地跳過
  所有文字辨識**。症狀是「譜上印的 ♩=179 讀不到」，而 MusicXML 上完全看不出少了東西。
  放在唯讀的 `/opt/tessdata` 並設 `TESSDATA_PREFIX`；**不能放 `$HOME`**（runtime 的
  `HOME` 指到 tmpfs，build 階段寫進去的東西執行時根本不在那裡）。

### homr

- **0.7.0 跑 onnxruntime，沒有 torch。** 權重 157 MB（segnet 57 + encoder 53 +
  decoder 47），rapidocr 的 OCR 權重 32 MB 直接內建在 wheel 裡。
- **`homr --init` 就是烘權重的那一步**，說明字面寫著「if you want to prepare for
  example a Docker image」。它也是唯一會一併抓 OCR 權重的路徑，而且不需要放樣本圖
  跑一次推論（於是有版權問題的樂譜不必進 image）。
- **權重落在 site-packages 內部**（`homr/transformer/*.onnx`、`homr/segmentation/*.onnx`）。
  runtime 是唯讀 rootfs，所以 Dockerfile **明確驗證那些檔案存在** —— `--init` 沒生效
  的話 runtime 會試著往唯讀路徑下載然後失敗，而錯誤訊息指不到真正的成因。
- **輸出寫在輸入檔旁邊**（`<input>.musicxml`），另外還吐一個 `<input>_teaser.png`
  （辨識結果的視覺化，對除錯很有用）。所以輸入必須先複製到自己的暫存目錄。
- **`--output-tempo` 單獨給是 no-op。** `build_add_time_direction()` 第一行就是
  `if not args.metronome: return None`，整個 `<direction>` 區塊（`<sound tempo>` 也在
  裡面）只在給了 `--output-metronome` 時才產生。所以 worker **兩個旗標都傳**，同一個值。
- **`musicxml` 套件用 `ET.parse()` 讀 XSD 沒有指定編碼**，走 locale 預設。非 UTF-8
  locale 上直接炸 `UnicodeDecodeError`（在繁中 Windows 的 cp950 上實測到）。
  Dockerfile 與 subprocess 環境都設了 `PYTHONUTF8=1`。
- **`opencv-python` 與 `opencv-python-headless` 會同時被裝進來**（homr 要 headless，
  它相依的 rapidocr 要非 headless），兩者裝到同一個 `cv2` 路徑。所以 image 裡裝了
  `libgl1` 與 `libglib2.0-0`。
- **0.7.0 沒有 `--no-title`**（那是 main 分支才有的），標題 OCR 關不掉，而且不太可靠：
  實測三頁分別認出編曲者名、空字串、正確標題。

---

## 改這個 repo

- `*/app/main.py` **兩個 worker 刻意幾乎一樣**（對外契約一致，呼叫端不該需要知道背後
  是哪個引擎）。引擎的差異全部關在 `app/engine.py` 裡。**改路由或錯誤處理時兩邊要一起改。**
- 兩個 `engine.py` 的契約相同：`ENGINE` / `ready()` / `find_binary()` / `transcribe()`
  與三個例外（`RecognitionFailed` / `EngineTimeout` / `EngineMissing`）。想加第三個
  引擎的話，照著這個介面寫一個就好。
- 換行**一律 LF**（`.gitattributes` 釘住）。所有東西都在 Linux 容器裡跑，而 CRLF 的
  `.sh` 會變成 `bad interpreter: …^M`、CRLF 的 Dockerfile heredoc 會變成 Python 語法錯誤
  —— 症狀跟「腳本寫錯了」一模一樣，而且只在別人的機器上出現。
- **樣本圖與 OMR 輸出永遠不進版控**（見〈測試腳本〉）。

---

## English summary

Containerized OMR (Optical Music Recognition) workers: sheet-music image or PDF in,
MusicXML out, over a single multipart HTTP endpoint. Two interchangeable engines behind
an identical contract — [Audiveris](https://github.com/Audiveris/audiveris) 5.11.0
(default, ~4–8 s/page, accepts PDF) and [homr](https://github.com/liebharc/homr) 0.7.0
(fallback). Single-job by design (returns `429` when busy — queuing is the caller's job),
non-root, read-only rootfs, and **no outbound network at runtime**: models and OCR data
are baked into the image at build time.

`docker compose up -d --build`, then `POST /transcribe` with `file=@page.png`.
The prose is in Traditional Chinese, but the HTTP contract, error codes, and environment
variables are all in the tables above.

**AGPL-3.0** — both engines are AGPL, so this is too. If you run it as a network service,
§13 applies to you. Audiveris is shipped **modified**: one file, an 80-line diff in
[`audiveris-worker/patches/`](audiveris-worker/patches/single-bar-repeat.patch), applied
at build time so that a forward repeat `|:` whose heavy and light strokes are printed
without a gap is still recognised.
