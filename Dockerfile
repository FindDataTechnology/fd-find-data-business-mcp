# fd-find-data-business-mcp image.
#
# Base: the fd-open-data-mcp:torch image (published by the fd-open-data-mcp
# Jenkins job to the mesh Harbor). It carries the CPU-only torch build,
# sentence-transformers, and the all-MiniLM-L6-v2 model baked into
# /home/appuser/.cache/huggingface — so ai_search works with no network at
# runtime, and this CN-side build never has to fetch the multi-GB CUDA torch
# wheel from PyPI. The base also ships the /app/alembic schema-revision chain
# and /app/alembic.ini, which this Dockerfile removes: the gate must read the
# chain belonging to the fd-open-data-mcp that is actually installed here
# (resolved from the package's own alembic data), not the one from whenever
# the base was built.
#
# Standing coupling: this image follows the base's moving :torch tag; a
# rebuilt base is picked up by the next build here.
FROM hkccr.ccs.tencentyun.com/finddata/fd-open-data-mcp:main

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1

# Install the business package into the base (system python). Tsinghua
# mirror: bare PyPI from CN cloud boxes stalls for tens of minutes.
#
# /src, not the base's /build: the base image is built from the
# fd-open-data-mcp source tree, so it leaves that project (and its own
# build/lib output) in /build. Building this wheel there made setuptools
# assemble build/lib, which still held the base's fd_open_data_mcp tree — the
# wheel swallowed 253 of those files and, installing after fd-open-data-mcp
# 0.5.21, overwrote it with 0.5.16 code. The base's leftovers are removed too,
# so the running image holds one copy of each package.
USER root
RUN rm -rf /build
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple/ . \
    && rm -rf /src /app/alembic /app/alembic.ini

# Back to the base's unprivileged user.
USER appuser
WORKDIR /app

# 线自描述端点 /.well-known/line-manifest.json（change converge-line-identity-and-keys
# 1.4）：仓根那份是唯一真源，端点原样回吐。上面的 `COPY . .` 只把清单放进 /src，而
# /src 在 pip install 之后被删掉；setuptools 的 wheel 也不含仓根的 line-manifest.json
# （packages.find 只收包内文件）。所以必须显式 COPY 到运行期 WORKDIR（/app）——
# 不然镜像里这个文件根本不存在，端点永远 404。
COPY line-manifest.json ./

EXPOSE 8310
ENTRYPOINT ["fd-find-data-business-mcp", "serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8310"]
