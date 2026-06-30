FROM python:3.11-slim

WORKDIR /srv
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY app ./app

ENV PORT=8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
