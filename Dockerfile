FROM python:3.12-slim AS builder
WORKDIR /build
COPY . .
# Tsinghua mirror: bare PyPI from CN cloud boxes stalls for tens of minutes.
RUN pip install --no-cache-dir --prefix=/install \
    -i https://pypi.tuna.tsinghua.edu.cn/simple .

FROM python:3.12-slim
COPY --from=builder /install /usr/local
RUN useradd --create-home --uid 1000 appuser
USER appuser
EXPOSE 8310
ENTRYPOINT ["fd-find-data-business-mcp", "serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8310"]
