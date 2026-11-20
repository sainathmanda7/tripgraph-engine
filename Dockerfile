FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY eval ./eval
COPY tests ./tests

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e ".[dev]"

EXPOSE 8000
CMD ["uvicorn", "tripgraph.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
