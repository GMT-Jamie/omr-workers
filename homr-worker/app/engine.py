"""homr 的呼叫封裝。

## 為什麼是 subprocess 而不是 `import homr`

spec §4 偏好 Python API，而 homr 確實有一個可以呼叫的進入點
（`homr.main.process_image(image_path, config, xml_generator_args) -> None`）。
但那是**內部 API，沒有穩定性承諾**，而 `homr` 這支 CLI 是它 documented 的介面。

先走 subprocess，因為那個代價要先量出來才知道值不值得換。**已經量了一次**（開發機
原生，n=1，還沒在容器裡重量）：

    3 個行程各處理 1 張    72 秒 ÷ 3 趟 = 24.0 秒/趟
    1 個行程處理 3 張×2    104 秒 ÷ 6 趟 = 17.3 秒/趟   →  差 6.7 秒/頁，約 28%

🔴 上面那個「÷ 2」是**錯的**（原本寫「目錄模式會把每張圖處理兩遍，每張 10 次 tromr
推論而不是 5 次」）。容器裡數過實際次數：單檔 1 張是 Segnet 1 次／Tromr 5 次，目錄模式
3 張是 Segnet 3 次／Tromr 15 次 —— **正好 3 倍，每張只跑一遍**。誤判來自 `Processing`
在目錄模式出現 4 次：3 張圖各一次，再加一行標頭 `Processing 3 files: [...]`。

容器裡重量的結果（24 核、不限 CPU）：

    A  3 個行程各 1 張   40 秒（14+12+14） = 13.3 秒/頁，每頁都付一次冷啟動
    B  1 個行程 3 張     31 / 35 秒（兩次） = 3 頁共用一次冷啟動
    A − B = 2 × 冷啟動  →  冷啟動 ≈ 2.5 ～ 4.5 秒，佔單頁的 19% ～ 34%

**注意 bench.sh 的 `total - inference` 差不是這個數字**：容器裡那個差是 1 ms
（FastAPI 的請求開銷），因為 `inference` 量的是整個 subprocess，session 建立就在
它裡面。

**決定（2026-08-24）：維持 subprocess，不改成 import 內部 API。**

`import homr.main.process_image` + lifespan 預熱能省下那 2.5～4.5 秒，代價是綁一個沒有
穩定性承諾的內部函式。不做的理由是量完才看清楚的：**冷啟動的佔比只在核心多的時候才高，
而那正是總時間已經很短的時候** —— 推論隨核心數平行加速，ONNX session 建立大部分不會。
24 核上它佔 13.3 秒裡的 3 秒，2 核上單頁要 ~200 秒、同樣那 3 秒不值一提。而正式機是
核心較少的那一端，也就是這個最佳化最沒有用的地方。（低核心數下的冷啟動沒有實測，
這一句是推論；真要做之前先在正式機的核心數下量一次。）

`ready` 的語意也不因此改變：subprocess 架構下沒有常駐的模型，`ready` 只能是「執行檔與
權重都在」。而權重是 build 階段烘進 image 且有驗證閘門的，這個語意誠實得夠用。

## homr 0.7.0 的實測行為（全部從真實執行量來的，不是猜的）

- 輸出寫在**輸入檔旁邊**：`<input>.musicxml`，另外還會吐一個 `<input>_teaser.png`
  （辨識結果的視覺化）與 `--write-staff-positions` 時的 `.txt`。所以輸入必須先複製
  到一個自己的暫存目錄裡，否則會污染呼叫端的檔案。
- 權重放在**套件目錄內部**（`homr/transformer/*.onnx`、`homr/segmentation/*.onnx`），
  build 階段 `homr --init` 抓下來烘進 image。runtime 是唯讀 rootfs，所以那時候再
  下載會失敗，而錯誤訊息不會說出真正的原因 —— Dockerfile 因此明確驗證檔案存在。
- **`--output-tempo` 是「把你給的 BPM 寫進 MusicXML」，不是辨識譜上的速度記號。**
  實測三頁全部 `sound=0`，譜上印的 ♩=179 拿不到。所以 BPM 必須由呼叫端傳進來。
- **0.7.0 沒有 `--no-title`**（那是 main 分支才有的），標題 OCR 關不掉。
- 它會宣布 `Removing tuplets from measure # N` 然後把連音刪掉，MusicXML 上沒有
  痕跡。那幾行要撈出來當 warnings —— 而它們在 **stderr**，不是 stdout（homr 用
  一個 eprint 助手，所有訊息都走 stderr，包含 `Result was written to …`）。
- **`--output-tempo` 單獨給是 no-op。** `build_add_time_direction()` 開頭就是
  `if not args.metronome: return None`，所以必須配 `--output-metronome`。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ENGINE = "homr"

# homr 沒有 `--version`，版本從套件 metadata 拿。
try:
    ENGINE_VERSION = version("homr")
except PackageNotFoundError:      # pragma: no cover - 只有沒裝好時會走到
    ENGINE_VERSION = "unknown"

# 這兩個目錄裡必須有 .onnx，否則模型沒烘進 image。`/health` 的 ready 看它。
_WEIGHT_DIRS = ("homr/transformer", "homr/segmentation")

# stdout 上的 `Removing tuplets from measure # 22`
_TUPLET_LINE = re.compile(r"Removing tuplets from measure #\s*(\d+)")

# 錯誤訊息裡的 stderr 摘要截斷長度（spec §5）
_STDERR_CAP = 2000


class RecognitionFailed(RuntimeError):
    """引擎跑完了但沒有產出可用的 MusicXML → 422。"""


class EngineTimeout(TimeoutError):
    """超過單 job 時限 → 504。"""


@dataclass(frozen=True)
class Result:
    musicxml: str
    inference_ms: int
    warnings: list[str]


def _site_packages() -> Path:
    import homr

    return Path(homr.__file__).resolve().parent.parent


def ready() -> bool:
    """`/health` 的 `ready`:執行檔在 + 權重在。

    兩者都查,因為兩種缺失的症狀在請求期是一樣的(500),而在啟動期是可以分開講的。
    """
    return find_binary() is not None and weights_present()


def weights_present() -> bool:
    """權重是不是真的在 image 裡。

    **不做任何網路存取** —— runtime 沒有 outbound 網路（compose 的 internal network
    ＋ Hyper-V 的 internal switch），而這個函式的意義正是「不連網也能回答 ready」。
    """
    root = _site_packages()
    return all(any((root / d).glob("*.onnx")) for d in _WEIGHT_DIRS)


class EngineMissing(RuntimeError):
    """`homr` 執行檔找不到 —— 啟動時就該發現,不該變成每個請求的 500。"""


def find_binary() -> str | None:
    """`homr` 執行檔的路徑,找不到回 None。

    先看 PATH（容器裡 `pip install` 會把 console script 放進 `/usr/local/bin`),
    再看 `sys.executable` 旁邊的 Scripts/bin —— **venv 沒有 activate 時 PATH 上不會
    有它**,而那正是原生測試時踩到的坑:退回字面 `"homr"` 讓一個「執行檔不存在」
    變成請求期的 `FileNotFoundError`(HTTP 500),而 `/health` 還回著 ready: true。

    `python -m homr` 不是選項:homr 沒有 `__main__.py`,它的進入點是 console script。
    """
    if found := shutil.which("homr"):
        return found
    here = Path(sys.executable).parent
    for name in ("homr", "homr.exe"):
        cand = here / name
        if cand.exists():
            return str(cand)
    return None


def _binary() -> str:
    if found := find_binary():
        return found
    raise EngineMissing("PATH 與 venv 裡都找不到 `homr` 執行檔")


def transcribe(
    data: bytes,
    filename: str,
    *,
    timeout_sec: int,
    tempo_bpm: int | None = None,
) -> Result:
    """圖片位元組 → MusicXML 字串。

    暫存目錄建在 `/tmp` 底下（唯讀 rootfs 下唯一可寫的地方），`finally` 保證刪掉
    —— 裡面有使用者上傳的樂譜圖片，留著是資料外洩。
    """
    # 副檔名要留著：homr 用它判斷格式，而輸出檔名是 `<輸入去掉副檔名>.musicxml`
    suffix = Path(filename).suffix.lower() or ".png"
    tmpdir = tempfile.mkdtemp(prefix="homr-")
    try:
        img = Path(tmpdir) / f"page{suffix}"
        img.write_bytes(data)

        cmd = [_binary()]
        if tempo_bpm is not None:
            # **`--output-tempo` 單獨給等於什麼都沒做。** homr 0.7.0 的
            # `build_add_time_direction()` 第一行就是 `if not args.metronome:
            # return None` —— 整個 `<direction>` 區塊（`<sound tempo>` 也在裡面）
            # 只在給了 `--output-metronome` 時才產生。實測 `--output-tempo 179`
            # 的輸出裡 `<sound>` 是 0 個。
            #
            # 所以兩個都給，同一個值。`--output-metronome` 單獨給也會寫出
            # `<sound tempo=同值>`（那個 else 分支），但明寫兩個才看得出意圖。
            bpm = str(int(tempo_bpm))
            cmd += ["--output-metronome", bpm, "--output-tempo", bpm]
        cmd.append(str(img))

        env = {
            **os.environ,
            # `musicxml` 套件用 `ET.parse(file)` 讀它的 XSD，**沒有指定編碼**，於是
            # 走 locale 預設。在非 UTF-8 locale 上直接炸 UnicodeDecodeError（實測在
            # 繁中 Windows 的 cp950 上就是這樣）。容器裡通常是 C.UTF-8 所以碰不到，
            # 但這一行的成本是零而漏掉它的除錯成本很高。
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }

        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_sec,
                cwd=tmpdir,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EngineTimeout(f"homr 超過 {timeout_sec} 秒未完成") from exc
        inference_ms = int((time.perf_counter() - t0) * 1000)

        out = Path(tmpdir) / f"{img.stem}.musicxml"
        if proc.returncode != 0 or not out.exists():
            tail = (proc.stderr or proc.stdout or "").strip()[-_STDERR_CAP:]
            raise RecognitionFailed(
                f"homr 結束碼 {proc.returncode}，沒有產出 MusicXML。stderr 尾段：{tail}"
            )

        musicxml = out.read_text(encoding="utf-8")
        # 空的或不像 MusicXML 就當辨識失敗。**不嘗試修補半壞的 XML**（spec §10）。
        if "score-partwise" not in musicxml and "score-timewise" not in musicxml:
            raise RecognitionFailed("homr 產出的檔案不是 MusicXML")

        return Result(
            musicxml=musicxml,
            inference_ms=inference_ms,
            # **homr 把所有訊息寫到 stderr，不是 stdout。** 兩邊都掃，因為上游哪天
            # 換過去我們不會知道 —— 而漏掉的後果是那些警告安靜地變成空陣列。
            warnings=_warnings_from(proc.stderr, proc.stdout),
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _warnings_from(*streams: str | None) -> list[str]:
    """把 homr 在 stdout 上宣布的靜默資料損失變成呼叫端看得見的東西。

    目前只有連音。實測那份三頁的譜：第 1、2 頁各 1 個小節，**第 3 頁 22 個小節裡
    有 10 個** —— 這不是邊角情況。

    收多個串而不是一個：**homr 把訊息寫到 stderr**（它用一個 eprint 助手，連
    `Result was written to …` 都走 stderr），但上游哪天搬到 stdout 我們不會知道，
    而漏掉的後果是那些警告安靜地變成空陣列。兩邊都掃的成本是零。
    """
    measures = [m for s in streams if s for m in _TUPLET_LINE.findall(s)]
    if not measures:
        return []
    return [
        "引擎刪掉了 {} 個小節裡的連音（第 {} 小節）".format(
            len(measures), "、".join(measures[:12]) + ("…" if len(measures) > 12 else "")
        )
    ]
