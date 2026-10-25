FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code edits don't invalidate this layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

# TODO (step 3): pre-download the fastembed embedding + rerank models here.

COPY . .

EXPOSE 8000
CMD ["python", "main.py", "serve"]
