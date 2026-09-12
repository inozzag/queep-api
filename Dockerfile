FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip install --no-cache-dir uv==0.10.12
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY queep ./queep
RUN useradd --uid 10001 --create-home queep
USER 10001
EXPOSE 8080
CMD [".venv/bin/uvicorn", "queep.main:app", "--host", "0.0.0.0", "--port", "8080"]
