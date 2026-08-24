"""homr-worker：FastAPI 服務、路由、單工閘門、timeout。

契約見 3ml 的 `docs/omr-workers-spec.md` §3。這個服務是**啞巴工人**：收圖、回
MusicXML，不含任何 3ml 的業務邏輯。排隊、配額、路由、MusicXML→MML 全部歸呼叫端。

## 單工是刻意的

容量 1 的 semaphore，滿載時**立即回 429，不排隊**。理由：單頁推論是 20 秒級的
CPU 佔用（實測 24 秒），排隊只是把「現在很忙」延後成「等了兩分鐘還是很忙」，而
呼叫端（3ml 後端）本來就是決定排隊策略的那一層。它看得到 429 才能決定要不要重試。

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

# homr 0.7.0 只吃點陣圖（PDF 支援是 0.7.0 之後才進 main 的，還沒發版）。
# PDF 是 audiveris-worker 的活。
ACCEPTED_SUFFIXES = {".png", ".jpg", ".jpeg"}

# 每種格式的魔術位元組。**副檔名不足以判斷** —— 一個改名成 .png 的 zip 會讓 homr
# 花 20 秒才失敗，而那 20 秒佔著唯一的工作位。400 要在讀檔的那一刻就回。
_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")


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
log = logging.getLogger("homr-worker")
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
    # 啟動時只檢查權重在不在，**不連網**（runtime 沒有 outbound 網路）。
    #
    # spec §4 要求「預載模型常駐記憶體」，走 subprocess 做不到 —— 每個請求是一個新
    # 行程。這是 engine.py 檔頭記下的 TODO(verify)：先量冷啟動佔比，再決定要不要
    # 改成 import 內部 API。所以這裡的 ready 語意是「引擎可執行」而不是「模型已載入」。
    _state["ready"] = engine.ready()
    _log(
        "started",
        engine=engine.ENGINE,
        engine_version=engine.ENGINE_VERSION,
        ready=_state["ready"],
        job_timeout_sec=JOB_TIMEOUT_SEC,
        max_concurrency=MAX_CONCURRENCY,
    )
    if not _state["ready"]:
        # 不讓它安靜地跑起來然後每個請求都 500 —— 那個症狀指不到成因。
        # 兩種缺失分開講,因為修法完全不同。
        if engine.find_binary() is None:
            log.error(
                "找不到 `homr` 執行檔,/transcribe 一定會失敗。venv 沒 activate?"
                "還是 pip install 沒成功?",
                extra={"fields": {}},
            )
        else:
            log.error(
                "權重不在 image 裡,/transcribe 一定會失敗。Dockerfile 的 "
                "`homr --init` 沒生效?",
                extra={"fields": {}},
            )
    yield


app = FastAPI(title="homr-worker", lifespan=lifespan, docs_url=None, redoc_url=None)


def _error(status: int, code: ErrorCode, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error_code": code.value, "message": message})


# ─── 路由 ───────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        engine=engine.ENGINE,
        engine_version=engine.ENGINE_VERSION,
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
        # spec §3 說骨架階段可以留空殼，但 `tempo_bpm` 不是想像中的需求：homr
        # **讀不出譜上印的速度記號**（`--output-tempo` 是「寫進去」不是「認出來」），
        # 所以不傳的話產出的 MusicXML 一定沒有 `<sound tempo>`。
        #
        # 0.7.0 沒有 `--no-title`（那是 main 才有的），所以標題 OCR 沒有開關可留。
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
                f"只接受 {'、'.join(sorted(ACCEPTED_SUFFIXES))}（PDF 請送 audiveris-worker）",
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

        # ── 魔術位元組。副檔名可以說謊，而說謊的代價是 20 秒的工作位（見 _MAGIC） ──
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
                engine_version=engine.ENGINE_VERSION,
                timing_ms=Timing(total=total_ms, inference=result.inference_ms),
                warnings=result.warnings,
            ).model_dump()
        )
