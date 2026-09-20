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
FROM 100.64.0.8:30880/finddata/fd-open-data-mcp:torch

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1

# Install the business package into the base (system python). Tsinghua
# mirror: bare PyPI from CN cloud boxes stalls for tens of minutes.
USER root
WORKDIR /build
COPY . .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple/ . \
    && rm -rf /build /app/alembic /app/alembic.ini

# Back to the base's unprivileged user.
USER appuser
WORKDIR /app
EXPOSE 8310
ENTRYPOINT ["fd-find-data-business-mcp", "serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8310"]
