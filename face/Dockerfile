# face 服务镜像：权重烤进镜像，离线可用。
# 构建参数：
#   PIP_INDEX_URL     可选，pip 镜像（如 https://mirrors.huaweicloud.com/repository/pypi/simple）
#   WEIGHTS_BASE_URL  可选，权重下载基址（默认 hf-mirror）
ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

ARG PIP_INDEX_URL
ARG WEIGHTS_BASE_URL=https://hf-mirror.com/serengil/deepface/resolve/main

# opencv-headless / onnxruntime 运行库（headless 不依赖 libGL）
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates libglib2.0-0 libgomp1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --index-url "$PIP_INDEX_URL" -r requirements.txt; \
    else \
        pip install -r requirements.txt; \
    fi

COPY scripts ./scripts
COPY app ./app

# 烤入 4 份 ONNX 权重（facenet512 / retinaface / 两份 MiniFASNet）
RUN WEIGHTS_BASE_URL="$WEIGHTS_BASE_URL" python scripts/fetch_weights.py

EXPOSE 8500

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8500/health', timeout=5).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8500"]
