"""Audiveris CLI 的包裝。

契約與 homr-worker 的 engine.py 完全相同（`ENGINE` / `ready()` / `find_binary()` /
`transcribe()` 與那三個例外），所以 main.py 兩邊幾乎一樣 —— 呼叫端也不需要知道
背後換了引擎（spec §3）。

## Audiveris 5.11.0 的實測行為（全部從真實執行量來的，不是猜的）

- **輸出是 `.mxl`**（zip 過的 MusicXML），不是裸 XML。主檔名寫在 zip 裡的
  `META-INF/container.xml`，不要假設它叫 `<輸入>.xml`。
- 同時還會吐一個 `.omr`（Audiveris 自己的 book 檔，實測 122 KB）與一份 `.log`。
  它們落在 `-output` 指定的目錄裡，跟著暫存目錄一起刪掉。
- **它會讀譜上印的速度記號** —— 實測 T1 拿到 `<sound tempo="179">`，那正是 homr
  拿不到的東西。⚠️ **但這件事完全取決於 OCR 語言資料是不是 legacy 版**，見 Dockerfile。
- **它把連結線標成 `<tied>`**（實測 28 條），不像 homr 全部標成 `<slur>` 逼下游
  用「兩端音高相同」去猜。
- `MeasureFixer` 會在 log 上宣布哪些小節的時值湊不出一個完整小節。那是這個引擎
  唯一會講出來的靜默資料問題，要撈出來當 warnings。
- 它**不吃 stdin**，只吃檔案路徑；輸出目錄要自己給。
- **反覆記號兩個方向都會出**（`<repeat direction="forward">` 與 `"backward"`）。
  ⚠️ 原版的 `forward` 在某些排版上會整批消失 —— image 裡打了補丁，見 Dockerfile
  的〈補丁〉那一段。**別依賴上游未修改的 Audiveris 有這個行為。**

## 跟 homr 的實測對照（同一批三頁鋼琴譜，66 小節）

    壞小節      homr 28 (42%)   audiveris 19 (29%)
    連音        homr 0（引擎自己刪了 11 個小節）  audiveris 5 保留
    速度記號    homr 讀不到      audiveris ♩=179 讀到了
    4 核單頁    homr 95.9 秒     audiveris 4.1 秒
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

ENGINE = "audiveris"

# .deb 裝在這裡（jpackage 的固定版面）。PATH 上沒有它 —— 套件不建 /usr/bin 的連結。
_INSTALL = "/opt/audiveris/bin/Audiveris"

# `Audiveris -version` 的第二行：`- Version:      5.11.0`
_VERSION_LINE = re.compile(r"^-\s*Version:\s*(\S+)", re.MULTILINE)

# log 上的 `System#1 No target duration for measures local IDs [3, 5, 7], please check
# time signatures`
#
# ⚠️ **這句話的意思是「我不知道一小節該多長，所以檢查不了」，不是「這些小節壞了」。**
# 原文最後那半句 `please check time signatures` 就是在講這件事。實測：第 1 頁有拍號
# → 一條都不出；第 2、3 頁沒有拍號（印刷譜只在第一頁印）→ **整頁每一個小節都被列出來**。
#
# 第一版把它翻成「時值湊不出一整個小節」，於是一份實際只有 9 和 5 個壞小節的譜，
# 警告說 23/23 和 22/22 全壞。誤譯一個引擎訊息就能製造這種災難，所以下面只講
# 「沒有拍號、無法檢查」，而且不列小節號（列出來只是噪音）。
_NO_TIME_SIG = re.compile(r"No target duration for measures local IDs \[([^\]]*)\]")

# OCR 沒真的起來時的兩種說法。**兩種都要抓** —— 它們的差別是「檔案不在」與
# 「檔案在但不是 legacy 版」，而後者的症狀是**安靜地跳過所有文字辨識**。
_OCR_DEAD = re.compile(
    r"No installed OCR languages|Could not initialize TessBaseAPI|Failed loading language"
)

# 錯誤訊息裡的 stderr 摘要截斷長度（spec §5）
_STDERR_CAP = 2000

# tessdata 的位置。Dockerfile 把它烘在唯讀的 /opt 並設這個環境變數 —— 不能放
# $HOME，runtime 的 HOME 指到 tmpfs，build 階段寫進去的東西執行時不在那裡。
_TESSDATA = Path(os.environ.get("TESSDATA_PREFIX", "/opt/tessdata"))

# 「這張圖上沒有五線譜」。**這是使用者最容易踩到的失敗**（傳錯檔案、翻拍太糊、
# 解析度太低），所以它值得一句人話而不是一段 stack trace。
#
# Audiveris 自己把原因講得很清楚，而且正好就是使用者能自己處理的那兩件事：
#
#     WARN [x] SheetStub 411 | With a too low interline value of 2 pixels,
#     either this sheet contains no multi-line staves, or the picture
#     resolution is too low (try 300 DPI). This interline value is NOT RELIABLE!
#     INFO [x] SheetStub 1194 | Sheet x flagged as invalid.
#     Caused by: StepException: Sheet ignored
#
# ⚠️ **那段話在輸出的最上面，而失敗訊息取的是最後 2000 字** —— 所以在加這條之前，
# 使用者拿到的是被截斷剩下的 stack frame 加一個容器內的暫存路徑，真正的診斷剛好
# 被切掉。實測整頁文字、照片雜訊、全白三種圖走的都是這條路徑。
#
# 三個樣式都留著：`interline` 是診斷本身，另外兩個是同一件事在 log 上的其他說法
# （不同的圖會停在不同的地方）。
_NO_STAVES = re.compile(
    r"too low interline value|flagged as invalid|StepException: Sheet ignored"
)


class RecognitionFailed(RuntimeError):
    """引擎跑完了但沒有產出可用的 MusicXML → 422。"""


class EngineTimeout(TimeoutError):
    """超過單 job 時限 → 504。"""


class EngineMissing(RuntimeError):
    """Audiveris 執行檔找不到 —— 啟動時就該發現,不該變成每個請求的 500。"""


@dataclass(frozen=True)
class Result:
    musicxml: str
    inference_ms: int
    warnings: list[str]


def find_binary() -> str | None:
    """Audiveris 執行檔的路徑,找不到回 None。

    先看安裝路徑再看 PATH —— .deb **不建 /usr/bin 的符號連結**，所以 `which` 找不到
    它。這個順序跟 homr-worker 相反是刻意的（那邊是 pip 的 console script，本來就
    在 PATH 上）。

    ⚠️ **絕對不要退回字面 `"Audiveris"`。** 那會把一個「沒裝好」的部署錯誤變成
    請求期的 `FileNotFoundError`（HTTP 500），而 `/health` 還回著 ready: true。
    """
    if Path(_INSTALL).exists():
        return _INSTALL
    return shutil.which("Audiveris") or shutil.which("audiveris")


def _binary() -> str:
    if found := find_binary():
        return found
    raise EngineMissing(f"找不到 Audiveris 執行檔（找過 {_INSTALL} 與 PATH）")


def ocr_ready() -> bool:
    """OCR 的語言資料在不在。

    ⚠️ **只檢查檔案存在，檢查不出它是不是 legacy 版** —— 而「不是 legacy 版」正是
    那個最隱蔽的失敗（檔案在、不報錯、安靜跳過所有文字辨識，於是譜上的速度記號
    拿不到）。那一層由 Dockerfile 的 checksum 與 build 階段的驗證步驟守。
    """
    return _TESSDATA.is_dir() and any(_TESSDATA.glob("*.traineddata"))


def ready() -> bool:
    """`/health` 的 `ready`：執行檔在 + OCR 語言資料在。

    兩者都查，因為兩種缺失的症狀在請求期完全不同（前者是 500，後者是**成功但
    少東西**），而在啟動期是可以分開講清楚的。
    """
    return find_binary() is not None and ocr_ready()


# ─── image 指紋 ──────────────────────────────────────────────────────────────
#
# **`engine_version` 分辨不出兩個不同的 image。** 它是 Audiveris 自己的版本
# （`-version` 印的 5.11.0），而我們的補丁是編一個 class 蓋在 classpath 前面 ——
# 它不會、也不該去動上游的版本字串。於是打過補丁與沒打過的 image，`/health` 的
# 每一個欄位都一模一樣。
#
# 踩過的實際情形：正式機更新完之後症狀完全沒變（一份 21 小節、m5–m12 反覆的譜還是
# 播成 33 小節而不是 29），而手上沒有任何東西可以分辨這三種可能 —— **補丁沒生效**、
# **image 沒重建**、還是**容器沒換成新 image**。下面兩個函式各回答一個。

_BUILD_ID = Path("/srv/BUILD_ID")

# 這個 image **應該**有的補丁：id → 它編出來的 class（相對於 patch 目錄）。
# 加補丁的時候這裡要跟著加一條，否則 `/health` 會漏報它。
_PATCHES = {
    "single-bar-repeat": "org/audiveris/omr/sig/inter/StaffBarlineInter.class",
}

_PATCH_DIR = Path("/opt/audiveris/lib/app/patch")

# jpackage 的啟動設定。補丁目錄必須插在 `audiveris.jar` **前面**才會優先載入，
# 所以「class 檔在」本身不構成「補丁生效」—— 見 Dockerfile 的〈補丁〉那一段。
_CFG = Path("/opt/audiveris/lib/app/Audiveris.cfg")

_CLASSPATH_LINE = "app.classpath=$APPDIR/patch"


def build_id() -> str:
    """這個 image 是什麼時候 build 的（UTC ISO-8601）。

    **不快取**：讀一個幾十 bytes 的檔案比記一個永遠不變的值划算，而 `/health`
    本來就不是熱路徑。讀不到回 `"unknown"` 而不是丟例外 —— 補丁之前的舊 image
    沒有這個檔，而「舊到連指紋都沒有」本身就是一個有用的答案。
    """
    try:
        return _BUILD_ID.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def patches() -> list[str]:
    """**實際生效**的補丁 id，照字母序。

    ⚠️ 這不是一份寫死的清單，是**查出來的**：class 檔在、而且 classpath 真的指到
    patch 目錄，兩個條件都成立才算數。寫死的字串在「有人把舊 image 重新打了一個新
    標籤」的時候會說謊，而那正是這個欄位存在要分辨的情況之一。

    classpath 那一行不見時回空清單，而不是照樣把 class 檔列出來：檔案躺在那裡但
    沒有人載它，效果跟沒有補丁完全相同，而「檔案在」會把查的人引去錯的方向。
    """
    try:
        wired = any(line.strip() == _CLASSPATH_LINE
                    for line in _CFG.read_text(encoding="utf-8").splitlines())
    except OSError:
        return []
    if not wired:
        return []
    return sorted(pid for pid, cls in _PATCHES.items() if (_PATCH_DIR / cls).is_file())


_version_cache: str | None = None


def engine_version() -> str:
    """`Audiveris -version` 的版本字串，快取起來。

    **不在 import 時算** —— 那要起一次 JVM（約 1 秒），而 import 可能發生在
    「只是想看看 /health 為什麼不 ready」的時候。找不到執行檔時回 "unknown"
    而不是丟例外，理由同上：診斷路徑不該自己先炸掉。
    """
    global _version_cache
    if _version_cache is not None:
        return _version_cache

    binary = find_binary()
    if binary is None:
        return "unknown"
    try:
        proc = subprocess.run(
            [binary, "-version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False,
        )
        if m := _VERSION_LINE.search(proc.stdout or ""):
            _version_cache = m.group(1)
            return _version_cache
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


# main.py 讀這個名字（跟 homr-worker 對齊）。用函式算出來,所以 import 時不起 JVM。
ENGINE_VERSION = "unknown"


def transcribe(
    data: bytes,
    filename: str,
    *,
    timeout_sec: int,
    tempo_bpm: int | None = None,
) -> Result:
    """圖片（或 PDF）位元組 → MusicXML 字串。

    暫存目錄建在 `/tmp` 底下（唯讀 rootfs 下唯一可寫的地方），`finally` 保證刪掉
    —— 裡面有使用者上傳的樂譜，留著是資料外洩。
    """
    # 副檔名要留著：Audiveris 用它判斷是圖片還是 PDF。
    suffix = Path(filename).suffix.lower() or ".png"
    tmpdir = tempfile.mkdtemp(prefix="audiveris-")
    try:
        src = Path(tmpdir) / f"page{suffix}"
        src.write_bytes(data)
        outdir = Path(tmpdir) / "out"
        outdir.mkdir()

        cmd = [
            _binary(),
            "-batch",        # 不開 GUI
            "-export",       # 產出 MusicXML（.mxl）
            "-output", str(outdir),
            str(src),
        ]

        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout_sec, cwd=tmpdir, env=env, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EngineTimeout(f"Audiveris 超過 {timeout_sec} 秒未完成") from exc
        inference_ms = int((time.perf_counter() - t0) * 1000)

        musicxml = _read_export(outdir)
        if musicxml is None:
            # 先問「是不是根本沒有五線譜」。這是使用者最常踩到的失敗,而且是他自己
            # 修得掉的 —— 換一張圖就好。⚠️ 下面那個退路會吐一整段引擎輸出,對他
            # 沒有任何意義,所以這一條要擋在它前面。
            if any(s and _NO_STAVES.search(s) for s in (proc.stdout, proc.stderr)):
                raise RecognitionFailed(
                    "這張圖裡找不到五線譜 —— 可能不是樂譜，"
                    "或是解析度太低（Audiveris 建議 300 DPI 以上）"
                )
            # 沒認出來的失敗才留原始輸出。**這一段是給維運看的,不是給使用者看的** ——
            # 呼叫端對認得的錯誤碼會顯示自己的多語系字串,把這串丟進日誌。
            tail = (proc.stderr or proc.stdout or "").strip()[-_STDERR_CAP:]
            raise RecognitionFailed(
                f"Audiveris 結束碼 {proc.returncode}，沒有產出 MusicXML。輸出尾段：{tail}"
            )
        if "score-partwise" not in musicxml and "score-timewise" not in musicxml:
            raise RecognitionFailed("Audiveris 產出的檔案不是 MusicXML")

        warnings = _warnings_from(proc.stdout, proc.stderr)
        musicxml, tempo_note = _apply_tempo(musicxml, tempo_bpm)
        if tempo_note:
            warnings.append(tempo_note)

        return Result(musicxml=musicxml, inference_ms=inference_ms, warnings=warnings)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _read_export(outdir: Path) -> str | None:
    """從 `-output` 目錄裡把 MusicXML 挖出來。

    ⚠️ **主檔名要照 `META-INF/container.xml` 讀，不要猜。** 它目前確實叫
    `<book>.xml`，但那是 zip 內部的實作細節，而 MusicXML 的 `.mxl` 規格本來就規定
    用 container.xml 指路。猜錯的症狀是「辨識成功但回傳空字串」。

    ⚠️ **多個 `.mxl` 要明確失敗，不能只回第一個。** Audiveris 對多樂章的 book 會
    吐 `<book>.mvt1.mxl`、`<book>.mvt2.mxl`，而「回第一個」的症狀是**安靜地只拿到
    第一樂章** —— 回傳的 MusicXML 完全合法，呼叫端與使用者都看不出少了一半。

    實測：一份**七頁**的 PDF（單樂章）只吐一個 `score7.mxl`，所以正常的多頁 PDF
    不會走到這一條。多樂章的情況沒有樣本可測，因此選擇「講出來」而不是猜。

    **也不在這裡合併。** 合併 MusicXML 是縫合（part-list 要對、divisions 可能不同、
    小節要重編號），而縫合的正本在呼叫端的 `musicxml-in.js` —— 在這裡寫第二份
    等於在授權邊界的另一邊放一份會漂移的邏輯（見 README 的〈AGPL 邊界〉）。
    """
    mxls = sorted(outdir.glob("*.mxl"))
    if len(mxls) > 1:
        raise RecognitionFailed(
            f"這份樂譜被判成 {len(mxls)} 個樂章，請把樂章分開上傳"
        )

    for mxl in mxls:
        try:
            with zipfile.ZipFile(mxl) as zf:
                inner = _rootfile_of(zf)
                if inner:
                    return zf.read(inner).decode("utf-8", errors="replace")
        except (zipfile.BadZipFile, KeyError, OSError):
            continue

    # 退路：有些版本／設定會直接吐裸 XML。
    for xml in sorted(outdir.glob("*.xml")):
        return xml.read_text(encoding="utf-8", errors="replace")
    return None


def _rootfile_of(zf: zipfile.ZipFile) -> str | None:
    try:
        root = ET.fromstring(zf.read("META-INF/container.xml"))
    except (KeyError, ET.ParseError):
        # 沒有 container.xml：退回「唯一那個不在 META-INF 底下的 .xml」
        names = [n for n in zf.namelist()
                 if n.lower().endswith(".xml") and not n.startswith("META-INF/")]
        return names[0] if len(names) == 1 else None

    for rf in root.iter("rootfile"):
        if path := rf.get("full-path"):
            return path
    return None


def _apply_tempo(musicxml: str, tempo_bpm: int | None) -> tuple[str, str | None]:
    """處理速度。回傳 (musicxml, 要不要跟使用者說一句話)。

    規則:**譜上讀到的贏。**

    Audiveris 真的認得印在譜上的速度記號（實測 ♩=179），而那比呼叫端傳來的預設值
    可靠得多 —— 呼叫端那個值多半是 UI 上的預設 120，使用者根本沒動過它。所以只有
    在譜上什麼都沒讀到的時候才注入。

    ⚠️ 但這件事**必須說出來**：使用者在框裡填了 120、聽到的卻是 179，不講的話那是
    一個他無法解釋的現象。所以偵測到而且跟他填的不一樣時回一句話給 warnings。
    """
    detected = re.search(r'<sound[^>]*\btempo="([\d.]+)"', musicxml)

    if detected:
        if tempo_bpm is not None and abs(float(detected.group(1)) - tempo_bpm) >= 1:
            return musicxml, (
                f"譜上印的速度是 ♩={_pretty(detected.group(1))}，"
                f"已經照它播（你填的 {tempo_bpm} 沒有用到）"
            )
        return musicxml, None

    if tempo_bpm is None:
        return musicxml, None

    injected = _inject_tempo(musicxml, int(tempo_bpm))
    if injected is None:
        # 找不到可以插入的地方就原樣回去。**不要為了塞進去而亂改 XML** —— 下游
        # 會用預設速度播，那比拿到一份壞掉的 MusicXML 好。
        return musicxml, "沒辦法把速度寫進這份樂譜，播放時會用預設速度"
    return injected, None


def _pretty(v: str) -> str:
    return str(int(float(v))) if float(v).is_integer() else v


def _inject_tempo(musicxml: str, bpm: int) -> str | None:
    """把速度插進第一個 `<measure>`。

    用字串插入而不是重新序列化整份 XML:`ElementTree` 會丟掉 DOCTYPE、改變命名空間
    前綴、重排屬性 —— 那些對下游都是沒必要的變動,而我們只是要多一個元素。

    插在 `</attributes>` 後面（拍號、divisions 都宣告完了），沒有 `<attributes>`
    就插在第一個 `<measure ...>` 標籤後面。兩者都是 MusicXML 允許 `<direction>`
    出現的位置。
    """
    block = (
        "<direction placement=\"above\">"
        "<direction-type><metronome>"
        "<beat-unit>quarter</beat-unit>"
        f"<per-minute>{bpm}</per-minute>"
        "</metronome></direction-type>"
        f"<sound tempo=\"{bpm}\"/>"
        "</direction>"
    )

    if (at := musicxml.find("</attributes>")) != -1:
        cut = at + len("</attributes>")
        return musicxml[:cut] + block + musicxml[cut:]

    if m := re.search(r"<measure\b[^>]*>", musicxml):
        return musicxml[:m.end()] + block + musicxml[m.end():]

    return None


def _warnings_from(*streams: str | None) -> list[str]:
    """把 Audiveris 在 log 上宣布的問題變成呼叫端看得見的東西。

    兩件事值得說：

    1. **這一頁沒有拍號。** 印刷樂譜的拍號只在第一頁印，所以第 2 頁之後的圖上根本
       沒有它 —— 引擎因此無法自行檢查小節長度。⚠️ **這不代表小節壞了**（見
       `_NO_TIME_SIG` 上面那段），而且下游縫合多頁時會沿用前一頁的拍號，所以對
       最終結果沒有影響。講出來是因為使用者只送單頁時會想知道。

    2. **OCR 沒真的起來。** 檔案在但不是 legacy 版時它只留一行 log 然後安靜跳過
       所有文字辨識 —— 譜上的速度記號就這樣拿不到，而 MusicXML 上完全看不出少了
       東西。這是整個 image 裡最隱蔽的失敗，值得在每個請求上守著。

    ⚠️ **不要在這裡自己算「有幾個壞小節」。** 那需要拍號、divisions、跨頁沿用，
    而那三件事下游的 musicxml-in.js 已經完整做過了（它會出「有 N／M 個小節…」
    那條警告）。在這裡重算一份只會得到一個跟它不一致的數字。
    """
    out: list[str] = []

    if any(s and _NO_TIME_SIG.search(s) for s in streams):
        out.append("這一頁沒有偵測到拍號（印刷樂譜通常只印在第一頁），多頁匯入時會沿用前一頁的")

    if any(s and _OCR_DEAD.search(s) for s in streams):
        out.append("文字辨識沒有啟用，譜上印的速度記號與標題讀不到")

    return out
