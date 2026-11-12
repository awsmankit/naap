# Naap: drawing -> Measurement Book.  docker build -t naap . && docker run -p 8000:8000 -v naap-data:/data naap
#
# Debian has no LibreDWG package, so stage 1 builds dwg2dxf from the official release tarball.
# LibreDWG is GPL-3.0-or-later; Naap only runs it as a separate program (see THIRD_PARTY_NOTICES.md).

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- 1. LibreDWG (dwg2dxf)
FROM python:${PYTHON_VERSION}-slim-bookworm AS libredwg
ARG LIBREDWG_VERSION=0.14.8922
ARG LIBREDWG_SHA256=b8d28cb07824a1c33c57349b8f31d8debc21361789cdbd31e0c06030677164e2
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential ca-certificates curl xz-utils pkg-config \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fsSL -o libredwg.tar.xz \
      "https://github.com/LibreDWG/libredwg/releases/download/${LIBREDWG_VERSION}/libredwg-${LIBREDWG_VERSION}.tar.xz" \
 && echo "${LIBREDWG_SHA256}  libredwg.tar.xz" | sha256sum -c - \
 && tar -xJf libredwg.tar.xz --strip-components=1 \
 && ./configure --prefix=/opt/libredwg --disable-bindings --disable-static --disable-docs \
 && make -j"$(nproc)" \
 && make install \
 && /opt/libredwg/bin/dwg2dxf --version

# ---------------------------------------------------------------- 2. the app
FROM python:${PYTHON_VERSION}-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.9.14 /uv /usr/local/bin/uv
COPY --from=libredwg /opt/libredwg/bin/dwg2dxf /usr/local/bin/dwg2dxf
COPY --from=libredwg /opt/libredwg/lib/ /usr/local/lib/
RUN ldconfig && dwg2dxf --version \
 && useradd --create-home --uid 10001 naap \
 && mkdir -p /data && chown naap /data

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY engine ./engine
COPY engine_struct ./engine_struct
COPY convert ./convert
COPY web ./web
COPY samples ./samples
COPY LICENSE THIRD_PARTY_NOTICES.md README.md ./

# HOST/PORT: bind address (the app defaults to 127.0.0.1:8777 outside Docker).
# DWG_MB_DATA: where uploads and jobs are kept; mount a volume there.
ENV HOST=0.0.0.0 PORT=8000 DWG_MB_DATA=/data DWG2DXF=/usr/local/bin/dwg2dxf PATH="/app/.venv/bin:$PATH"
USER naap
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/meta', timeout=4)"
CMD ["python", "-m", "app"]
