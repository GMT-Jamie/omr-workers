"""audiveris-worker：FastAPI 服務、路由、單工閘門、timeout。

契約見 3ml 的 `docs/omr-workers-spec.md` §3。這個服務是**啞巴工人**：收圖、回
MusicXML，不含任何 3ml 的業務邏輯。排隊、配額、路由、MusicXML→MML 全部歸呼叫端。

⚠️ **這個檔案跟 homr-worker/app/main.py 是刻意幾乎一樣的。** 兩個 worker 對外的
契約完全相同（呼叫端不該需要知道背後是哪個引擎），差異全部關在 engine.py 裡。
改動路由或錯誤處理時**兩邊要一起改**。

## 單工是刻意的

容量 1 的 semaphore，滿載時**立即回 429，不排隊**。理由：一個請求整段佔著 CPU，
排隊只是把「現在很忙」延後成「等了兩分鐘還是很忙」，而呼叫端（3ml 後端）本來就是
決定排隊策略的那一層。它看得到 429 才能決定要不要重試。

⚠️ Audiveris 實測單頁 **4 秒**（而且對核心數幾乎無感：24 核 4.6 秒、4 核 4.1 秒、
2 核 5.2 秒），比 homr 快一個數量級。但單工的理由不變 —— 它仍然是整段佔用 CPU，
而且多頁樂譜是 N 個請求。

## 日誌落在 stdout

不是檔案。rootfs 是唯讀的，只有 `/tmp` 可寫，而寫進 tmpfs 的日誌重啟就消失 ——
那不叫日誌。`docker logs` 是唯一合理的去處。

**不記錄圖片內容**（spec §10）。檔名也不記 —— 使用者的檔名可能就是曲名，而那是
他上傳了什麼的直接線索。只記大小。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from . import engine
from .schemas import ErrorCode, HealthResponse, Timing, TranscribeResponse

# ─── 設定 ───────────────────────────────────────────────────────────────────

JOB_TIMEOUT_SEC = int(os.getenv("JOB_TIMEOUT_SEC", "600"))
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "1"))
PORT = int(os.getenv("PORT", "8080"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024

# Audiveris 吃點陣圖**與 PDF**（`Audiveris -batch` 直接認得 PDF，不需要我們先轉檔）。
# 這是它相對 homr 多出來的一項 —— 而使用者手上的乾淨譜多半就是 PDF。
ACCEPTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf"}

# 每種格式的魔術位元組。**副檔名不足以判斷** —— 一個改名成 .png 的 zip 會讓引擎
# 花好幾秒才失敗，而那幾秒佔著唯一的工作位。400 要在讀檔的那一刻就回。
_MAGIC = (
    b"\x89PNG\r\n\x1a\n",   # PNG
    b"\xff\xd8\xff",        # JPEG
    b"%PDF-",               # PDF
)


# ─── 結構化日誌 ─────────────────────────────────────────────────────────────

class _JsonLines(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "msg": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        # **traceback 一定要進去。** 少了它,一個 500 在日誌上就只有「未預期例外」
        # 五個字,而回應本身刻意不帶細節(那裡面可能有路徑)—— 於是沒有任何地方
        # 說得出成因。原生測試時就是這樣浪費了一輪。
        if record.exc_info:
            payload["traceback"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JsonLines())
log = logging.getLogger("audiveris-worker")
log.setLevel(logging.INFO)
log.addHandler(_handler)
log.propagate = False


def _log(msg: str, **fields: object) -> None:
    log.info(msg, extra={"fields": fields})


# ─── 生命週期 ───────────────────────────────────────────────────────────────

_gate = asyncio.Semaphore(MAX_CONCURRENCY)
_state: dict[str, object] = {"ready": False}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 啟動時只檢查本機檔案，**不連網**（runtime 沒有 outbound 網路）。
    #
    # spec §4 要求「預載模型常駐記憶體」，走 subprocess 做不到 —— 每個請求是一個新
    # 的 JVM。所以這裡的 ready 語意是「引擎可執行 + OCR 語言資料在」而不是
    # 「模型已載入」。Audiveris 單頁只要 4 秒，JVM 冷啟動已經含在裡面了，沒有
    # 「改成常駐」的動機（homr 那邊量過同一題，結論也是不改，見它的 engine.py）。
    _state["ready"] = engine.ready()
    _log(
        "started",
        engine=engine.ENGINE,
        engine_version=engine.engine_version(),
        ready=_state["ready"],
        job_timeout_sec=JOB_TIMEOUT_SEC,
        max_concurrency=MAX_CONCURRENCY,
    )
    if not _state["ready"]:
        # 不讓它安靜地跑起來然後每個請求都 500 —— 那個症狀指不到成因。
        # 兩種缺失分開講,因為修法完全不同。
        if engine.find_binary() is None:
            log.error(
                "找不到 Audiveris 執行檔,/transcribe 一定會失敗。.deb 沒裝成功?"
                "（Dockerfile 的 postinst 會因為 xdg-desktop-menu 失敗,見那裡的註解）",
                extra={"fields": {}},
            )
        else:
            log.error(
                "OCR 語言資料不在 image 裡。辨識**仍然會成功**,但譜上印的速度記號"
                "與標題讀不到,而 MusicXML 上看不出少了東西 —— 這是最隱蔽的失敗,"
                "所以擋在 ready 上。檢查 TESSDATA_PREFIX 與 Dockerfile 那一步。",
                extra={"fields": {}},
            )
    yield


app = FastAPI(title="audiveris-worker", lifespan=lifespan, docs_url=None, redoc_url=None)


def _error(status: int, code: ErrorCode, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error_code": code.value, "message": message})


# ─── 路由 ───────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        engine=engine.ENGINE,
        engine_version=engine.engine_version(),
        ready=bool(_state["ready"]),
    )


@app.post("/transcribe")
async def transcribe(
    request: Request,
    file: UploadFile = File(...),
    options: str | None = Form(None),
) -> JSONResponse:
    rid = uuid.uuid4().hex[:12]
    t0 = time.perf_counter()

    def done(status: str, **extra: object) -> None:
        _log(
            "transcribe",
            request_id=rid,
            status=status,
            total_ms=int((time.perf_counter() - t0) * 1000),
            **extra,
        )

    # ── 單工閘門。滿載立即 429，不排隊（見檔頭） ──
    if _gate.locked():
        done("busy")
        return _error(429, ErrorCode.BUSY, "worker 正在處理另一個請求，請稍後重試")

    async with _gate:
        # ── 選項。目前只有一個真參數 ──
        #
        # `tempo_bpm` 在這個 worker 是**退路**，不是主要來源：Audiveris 真的認得
        # 印在譜上的速度記號（實測 T1 拿到 `<sound tempo="179">`）。所以規則是
        # 「譜上讀到的贏，沒讀到才用呼叫端給的」—— 實作與理由見 engine._apply_tempo。
        #
        # ⚠️ 這一點跟 homr-worker 相反（那邊譜上的速度**永遠**讀不到，呼叫端傳的
        # 值是唯一來源）。呼叫端不需要知道差別：兩邊都接受 tempo_bpm，也都保證
        # 輸出裡有 `<sound tempo>`。
        tempo_bpm: int | None = None
        if options:
            try:
                parsed = json.loads(options)
                if not isinstance(parsed, dict):
                    raise ValueError("options 必須是 JSON 物件")
                raw = parsed.get("tempo_bpm")
                if raw is not None:
                    tempo_bpm = int(raw)
                    if not 1 <= tempo_bpm <= 1000:
                        raise ValueError("tempo_bpm 超出合理範圍")
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                done("invalid_options")
                return _error(400, ErrorCode.INVALID_INPUT, f"options 解析失敗：{exc}")

        # ── 副檔名 ──
        name = file.filename or ""
        suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if suffix not in ACCEPTED_SUFFIXES:
            done("bad_suffix", suffix=suffix)
            return _error(
                400,
                ErrorCode.INVALID_INPUT,
                f"只接受 {'、'.join(sorted(ACCEPTED_SUFFIXES))}",
            )

        # ── 大小。**邊讀邊算，不整塊吞進來** ──
        #
        # 先 `await file.read()` 再檢查等於讓任何人用一個 2 GB 的上傳把記憶體吃光，
        # 而 4 GB 的容器上限會讓 OOM killer 殺掉整個 worker 而不是回 413。
        chunks: list[bytes] = []
        size = 0
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                done("too_large", bytes=size)
                return _error(
                    413,
                    ErrorCode.FILE_TOO_LARGE,
                    f"檔案超過 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB 上限",
                )
            chunks.append(chunk)
        data = b"".join(chunks)

        if not data:
            done("empty")
            return _error(400, ErrorCode.INVALID_INPUT, "檔案是空的")

        # ── 魔術位元組。副檔名可以說謊，而說謊的代價是唯一那個工作位（見 _MAGIC） ──
        if not data.startswith(_MAGIC):
            done("bad_magic", bytes=size)
            return _error(400, ErrorCode.INVALID_INPUT, "檔案內容不是 PNG 或 JPEG")

        # ── 推論。subprocess 是阻塞的，丟到執行緒池，不然會卡住整個 event loop
        #     （於是連 /health 都不回應，而監控會判定容器死了） ──
        try:
            result = await asyncio.to_thread(
                engine.transcribe,
                data,
                name,
                timeout_sec=JOB_TIMEOUT_SEC,
                tempo_bpm=tempo_bpm,
            )
        except engine.EngineTimeout as exc:
            done("timeout", bytes=size)
            return _error(504, ErrorCode.TIMEOUT, str(exc))
        except engine.RecognitionFailed as exc:
            done("recognition_failed", bytes=size)
            return _error(422, ErrorCode.RECOGNITION_FAILED, str(exc))
        except engine.EngineMissing as exc:
            # 部署壞了,不是這個請求壞了。ready 應該已經是 false,但重查一次讓
            # `/health` 從此說實話（權重或執行檔可能是在啟動之後才消失的）。
            _state["ready"] = False
            log.error(str(exc), extra={"fields": {"request_id": rid}})
            done("engine_missing", bytes=size)
            return _error(500, ErrorCode.INTERNAL_ERROR, "worker 未正確安裝")
        except Exception as exc:                                    # noqa: BLE001
            # 例外細節進日誌，不進回應 —— 那裡面可能有路徑
            log.exception("未預期例外", extra={"fields": {"request_id": rid}})
            done("internal_error", bytes=size, error=type(exc).__name__)
            return _error(500, ErrorCode.INTERNAL_ERROR, "worker 內部錯誤")

        total_ms = int((time.perf_counter() - t0) * 1000)
        done(
            "ok",
            bytes=size,
            inference_ms=result.inference_ms,
            musicxml_bytes=len(result.musicxml),
            warnings=len(result.warnings),
        )
        return JSONResponse(
            content=TranscribeResponse(
                musicxml=result.musicxml,
                engine=engine.ENGINE,
                engine_version=engine.engine_version(),
                timing_ms=Timing(total=total_ms, inference=result.inference_ms),
                warnings=result.warnings,
            ).model_dump()
        )
