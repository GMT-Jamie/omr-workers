"""回應與錯誤的模型。

兩個 worker 完全相同 —— 呼叫端（一個 ASP.NET 後端）不應該需要知道背後是哪個
引擎。公開版的契約見 README 的〈HTTP 介面〉。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    """spec §3 的錯誤碼表。字串值就是回應裡的 `error_code`。"""

    INVALID_INPUT = "INVALID_INPUT"            # 400 非支援格式、檔案損毀、缺 file 欄位
    FILE_TOO_LARGE = "FILE_TOO_LARGE"          # 413 超過大小上限
    RECOGNITION_FAILED = "RECOGNITION_FAILED"  # 422 引擎跑完但產不出有效 MusicXML
    BUSY = "BUSY"                              # 429 已在處理另一個請求
    INTERNAL_ERROR = "INTERNAL_ERROR"          # 500 未預期例外
    TIMEOUT = "TIMEOUT"                        # 504 超過單 job 時限


class ErrorResponse(BaseModel):
    error_code: ErrorCode
    message: str


class HealthResponse(BaseModel):
    status: str = "ok"
    engine: str
    engine_version: str
    build: str = Field(
        default="unknown",
        description=(
            "image 的 build 時間（UTC ISO-8601）。**更新正式機之後先看這個欄位有沒有"
            "變** —— `engine_version` 是 Audiveris 自己的版本，兩個不同的 image 在"
            "那個欄位上長得一模一樣。`unknown` = 這個 image 舊到還沒有指紋。"
        ),
    )
    patches: list[str] = Field(
        default_factory=list,
        description=(
            "**實際生效**的 Audiveris 補丁 id（查出來的，不是寫死的，見 "
            "`engine.patches()`）。空的就表示這個 image 跑的是原封不動的上游 "
            "Audiveris —— 例如起始反覆 `|:` 會整批消失。"
        ),
    )
    ready: bool


class Timing(BaseModel):
    total: int = Field(description="收到請求到回應之間的毫秒數")
    inference: int = Field(description="引擎本身花的毫秒數（homr 是 subprocess 的執行時間）")


class TranscribeResponse(BaseModel):
    musicxml: str
    engine: str
    engine_version: str
    timing_ms: Timing
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "引擎自己丟掉了什麼。**這個欄位不是裝飾** —— homr 會在 stdout 上宣布"
            "「Removing tuplets from measure # N」然後把那些連音直接刪掉，而"
            "MusicXML 上沒有任何痕跡。實測一份三頁的譜，第 3 頁 22 個小節裡有 10 個"
            "被動過。不撈出來的話那件事對呼叫端與使用者都是完全不可見的。"
        ),
    )
