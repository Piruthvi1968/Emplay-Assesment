FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FASTEMBED_CACHE_PATH=/opt/models

WORKDIR /app

# Dependencies first so code edits don't invalidate this layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the embedding and re-rank models named in config.yaml into the image, so the first run needs
# no download. /opt/models is outside the mounted ./index volume, so the mount can't hide them.
# Only config.yaml is copied first: code edits then don't trigger a model re-download.
COPY config.yaml .
RUN python -c "import yaml; from fastembed import TextEmbedding; \
from fastembed.rerank.cross_encoder import TextCrossEncoder; \
c = yaml.safe_load(open('config.yaml')); \
TextEmbedding(c['embedding']['model'], cache_dir='/opt/models'); \
TextCrossEncoder(c['search']['rerank_model'], cache_dir='/opt/models')"

COPY . .

EXPOSE 8000
CMD ["python", "main.py", "serve"]
