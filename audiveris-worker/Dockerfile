# audiveris-worker
#
# 契約與 homr-worker **完全相同**（spec §3）—— 呼叫端不該需要知道背後是哪個引擎。
# 差異全部關在 app/engine.py 裡。
#
# 為什麼是 ubuntu 而不是 spec §5 寫的 `eclipse-temurin:21-jre-jammy`：
# 上游的 .deb 是 jpackage 打包的，**自帶 JRE**（lib/runtime/lib/server/libjvm.so），
# 而且它現在用 Java 25，那個 base image 既錯又多餘。用 ubuntu:24.04 是因為官方
# 剛好提供 ubuntu24.04 的 .deb，跟開發機的 WSL 同版。
FROM ubuntu:24.04

# 版本 pin + checksum。**兩者都要** —— GitHub 的 release 資產是可以被重新上傳的，
# 只 pin 版本號擋不住內容被換掉。
ARG AUDIVERIS_VERSION=5.11.0
ARG AUDIVERIS_DEB_SHA256=f20113aaa33b3149ec8d6a09b2a7963360e65fafd92d69389987a85bbc3ec7a3
# ⚠️ **一定要 tesseract-ocr/tessdata 這個 repo，不是 tessdata_fast/tessdata_best，
# 也不是 Ubuntu 的 tesseract-ocr-eng 套件。** 那幾個都是 LSTM-only，而 Audiveris 用
# **legacy 引擎**。症狀極度隱蔽：檔案在、啟動不報「沒有語言」，只在 log 裡留一行
# `Could not initialize TessBaseAPI languages: eng in legacy mode`，然後**安靜地
# 跳過所有文字辨識** —— 譜上印的速度記號（實測 ♩=179）就這樣拿不到，而 MusicXML
# 上完全看不出少了東西。實測換成這一份之後 `<sound tempo="179">` 就出現了。
ARG TESSDATA_SHA256=daa0c97d651c19fba3b25e81317cd697e9908c8208090c94c3905381c23fc047

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install --no-install-recommends -y \
        ca-certificates \
        curl \
        python3 \
        python3-pip \
        # ⚠️ **libgtk-3-0t64 不在 .deb 的 Depends 裡，但少了它程式根本起不來。**
        # Audiveris 在 `Main` 的**靜態初始化**就透過 JNA 載 gtk-3 做 HiDPI 縮放
        # （WellKnowns.enableHiDpiScaling），那發生在解析參數之前 —— `-batch` 救不了。
        # 症狀是 `UnsatisfiedLinkError: Unable to load library 'gtk-3'`，而 apt 裝
        # .deb 的時候一切正常，所以這個錯只在第一次執行時才出現。
        libgtk-3-0t64 \
    && rm -rf /var/lib/apt/lists/*

# ─── Audiveris 本體 ───
#
# ⚠️ `postinst` 會呼叫 `xdg-desktop-menu install` 註冊桌面選單，而容器裡沒有可寫的
# 系統選單目錄 → `dpkg` 回錯誤碼 3，**整個 build 失敗**（檔案其實已經裝好了）。
# 先把那個目錄建出來就沒事，不需要 `|| true` 那種把真錯誤一起吞掉的寫法。
RUN mkdir -p /usr/share/desktop-directories \
    && curl -fsSL -o /tmp/audiveris.deb \
        "https://github.com/Audiveris/audiveris/releases/download/${AUDIVERIS_VERSION}/Audiveris-${AUDIVERIS_VERSION}-ubuntu24.04-x86_64.deb" \
    && echo "${AUDIVERIS_DEB_SHA256}  /tmp/audiveris.deb" | sha256sum -c - \
    && apt-get update \
    && apt-get install --no-install-recommends -y /tmp/audiveris.deb \
    && rm -rf /tmp/audiveris.deb /var/lib/apt/lists/*

# ─── OCR 語言資料 ───
#
# 放在唯讀的 /opt 而不是 $HOME —— runtime 的 rootfs 是唯讀的、HOME 指到 /tmp，
# 烘進 $HOME 的東西執行時根本不在那裡。實測 Audiveris **認得 TESSDATA_PREFIX**，
# 所以不需要在啟動時把 23 MB 複製進 tmpfs。
RUN mkdir -p /opt/tessdata \
    && curl -fsSL -o /opt/tessdata/eng.traineddata \
        https://github.com/tesseract-ocr/tessdata/raw/main/eng.traineddata \
    && echo "${TESSDATA_SHA256}  /opt/tessdata/eng.traineddata" | sha256sum -c -

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    LANG=C.UTF-8 \
    # rootfs 唯讀，只有 /tmp 可寫（tmpfs）。Audiveris 會往 $HOME 底下寫它的設定與
    # 快取（.config/AudiverisLtd、.cache/AudiverisLtd），不指過去的話它在第一次
    # 執行就失敗，而錯誤訊息指不到「rootfs 唯讀」這件事。
    HOME=/tmp \
    XDG_CONFIG_HOME=/tmp/.config \
    XDG_CACHE_HOME=/tmp/.cache \
    TMPDIR=/tmp \
    TESSDATA_PREFIX=/opt/tessdata \
    PORT=8080 \
    JOB_TIMEOUT_SEC=600 \
    MAX_CONCURRENCY=1

WORKDIR /srv

COPY requirements.txt ./
# Ubuntu 24.04 的 pip 有 PEP 668 保護（externally-managed-environment）。這是容器，
# 整台機器就是這一個應用，所以直接放行比開一個 venv 再處理 PATH 單純。
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# ─── 驗證它真的跑得起來 ───
#
# **這一步不是保險，是必需的。** 上面 libgtk 那段註解說的問題（靜態初始化就載
# GTK）在 `apt install` 階段完全看不出來 —— 少一個函式庫的話 build 照樣成功，
# 然後**每一個請求**都 500。在 build 就爆掉比那好得多。
#
# 順便釘住 OCR：`-version` 會印出 OCR 資料夾，而語言載不起來時會有 WARN。
RUN /opt/audiveris/bin/Audiveris -version 2>&1 | tee /tmp/ver.txt \
    && grep -q "Version:[[:space:]]*${AUDIVERIS_VERSION}" /tmp/ver.txt \
    && ! grep -qi "No installed OCR languages" /tmp/ver.txt \
    && rm -f /tmp/ver.txt

COPY app ./app

# ─── non-root ───
RUN useradd --system --no-create-home --uid 10001 worker \
    && chown -R worker:worker /srv
USER worker

EXPOSE 8080

# 不裝 curl 也能做健康檢查 —— 少一個套件、少一個 CVE 來源。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import os,urllib.request,json,sys; \
r=json.load(urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/health\", timeout=4)); \
sys.exit(0 if r.get('ready') else 1)"

# workers=1：單工是刻意的（見 app/main.py 檔頭）。
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --no-access-log"]
