# homr-worker
#
# homr 0.7.0 跑 **onnxruntime，不是 torch**。實際 build 出來是 **1.51 GB**
# —— 比 spec 假設的「幾 GB」小，但也不是原本這行寫的「幾百 MB」。權重本身只佔
# 157 MB（build 時那個 .onnx 驗證步驟會印出來），大的是 onnxruntime 與**兩份
# opencv**（見下面 libgl1 那段：build log 證實 opencv-python 5.0.0.93 非 headless
# 版 73.8 MB 確實被一起拉下來）。
#
# 模型在 **build 階段**烘進 image，runtime 完全不連網（compose 的 internal network
# ＋ 正式機上 Hyper-V 的 internal switch）。

FROM python:3.12-slim

# ─── 為什麼需要這兩個 apt 套件 ───
#
# homr 相依 opencv-python-**headless**，但它相依的 rapidocr 拉的是 opencv-python
# （非 headless）。兩者裝到同一個 `cv2` 路徑，誰後裝誰生效 —— 也就是 `import cv2`
# 可能落在需要 libGL 的那一份上。實測環境裡兩個版本同時存在，所以這不是理論問題。
#
# 裝這兩個小套件比「pip uninstall opencv-python 賭 headless 頂上去」可靠：後者依賴
# 兩個套件的 cv2 介面完全等價，而那是一個沒人承諾的巧合。
RUN apt-get update && apt-get install --no-install-recommends -y \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # `musicxml` 套件用 `ET.parse(file)` 讀它的 XSD **沒有指定編碼**，走 locale 預設。
    # 非 UTF-8 locale 上直接炸 UnicodeDecodeError（在繁中 Windows 的 cp950 上實測到）。
    # 容器裡通常是 C.UTF-8 所以碰不到，但這兩行成本是零而漏掉它的除錯成本很高。
    PYTHONUTF8=1 \
    LANG=C.UTF-8 \
    # rootfs 唯讀，只有 /tmp 可寫（tmpfs）。任何往 $HOME 或 XDG 快取寫東西的相依
    # 都必須落在那裡，否則會在 runtime 才炸，而錯誤訊息指不到 rootfs 唯讀這件事。
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp \
    TMPDIR=/tmp \
    PORT=8080 \
    JOB_TIMEOUT_SEC=600 \
    MAX_CONCURRENCY=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ─── 權重：build 階段抓下來烘進 image ───
#
# `homr --init` 的說明字面就是「Downloads the models if they are missing and then
# exits. […] it's only useful if you want to prepare for example a Docker image.」
# —— 上游本來就是為這個用途做的。它也是**唯一**會一併抓 OCR 權重的路徑
# （`download_ocr_weights()` 只在 `--init` 下被呼叫）。
#
# spec §4 猜「可能要放一張樣本圖跑一次推論才會拉權重」—— 不需要，而且 `--init`
# 更好：不用樣本圖，於是版權有問題的樂譜不必進 image。
RUN homr --init

# ─── 驗證權重真的在裡面 ───
#
# **這一步不是保險，是必需的。** 權重落在 site-packages 內部
# （`homr/transformer/*.onnx`、`homr/segmentation/*.onnx`），而 runtime 的 rootfs
# 是唯讀的 —— 如果 `--init` 沒成功，runtime 會試著往唯讀路徑下載，然後失敗，
# 而錯誤訊息不會提到「模型沒烘進 image」這件事。在 build 就爆掉比那好得多。
RUN python - <<'PY'
import pathlib, sys, homr
root = pathlib.Path(homr.__file__).resolve().parent
total = 0
for sub in ("transformer", "segmentation"):
    found = sorted((root / sub).glob("*.onnx"))
    if not found:
        sys.exit(f"!! {root/sub} 裡沒有 .onnx —— `homr --init` 沒有生效")
    for f in found:
        total += f.stat().st_size
        print(f"  {f.stat().st_size:>12,}  {f.relative_to(root)}")
print(f"權重合計 {total/1e6:.0f} MB")
PY

COPY app ./app

# ─── non-root ───
#
# 建立在最後：前面幾步要寫 site-packages（權重就落在那裡），而 runtime 只需要讀。
RUN useradd --system --no-create-home --uid 10001 worker \
    && chown -R worker:worker /srv
USER worker

EXPOSE 8080

# 不裝 curl 也能做健康檢查 —— 少一個套件、少一個 CVE 來源。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,json,sys; \
r=json.load(urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/health\", timeout=4)); \
sys.exit(0 if r.get('ready') else 1)"

# workers=1：單工是刻意的（見 app/main.py 檔頭），多開 worker 會讓那個 semaphore
# 只管到自己的行程，於是 429 失效而兩個推論一起搶 2 顆 CPU。
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --no-access-log"]
