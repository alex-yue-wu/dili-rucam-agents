FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    tesseract-ocr \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /uvx /bin/

COPY pyproject.toml uv.lock README.md /app/
COPY src /app/src
COPY scripts /app/scripts
# The RUCAM item 6 cascade reads this workbook from the repository root. Without
# it the DILIrank tier cannot resolve any drug and every item 6 falls back to 0.
COPY dilirank_v2 /app/dilirank_v2

RUN uv sync --frozen --no-dev

ENTRYPOINT ["uv", "run", "python", "-m", "dili_rucam_agents.pipeline"]
CMD ["--help"]
