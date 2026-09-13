FROM python:3.12-slim

# uv for dependency management; DejaVu fonts for the roster image renderer
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

# dependencies first (cached layer), then the project
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY . .
RUN uv sync --frozen --no-dev

# state lives outside the image: out/ (events, reports) and any local fixtures
VOLUME ["/app/out", "/app/fixtures/25bg"]
CMD ["uv", "run", "oibot", "discord"]
