FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project
COPY main.py ./
ENV PYTHONUNBUFFERED=1
CMD ["uv", "run", "--no-sync", "main.py"]
