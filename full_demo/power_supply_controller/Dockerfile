FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# The venv lives outside /app so compose's bind mount of the source over /app
# doesn't hide it (or pick up the host's .venv).
ENV UV_PROJECT_ENVIRONMENT=/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-install-project
COPY . .

CMD ["uv", "run", "--locked", "main.py"]
