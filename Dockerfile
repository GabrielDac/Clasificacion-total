# Clasificación documental — la app completa: página + motor SRU + motor Z39.50.
# Docker es necesario porque el protocolo Z39.50 usa el cliente `yaz` del
# sistema, que el entorno Python nativo de Render no ofrece.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends yaz \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD gunicorn -b 0.0.0.0:${PORT:-10000} -w 2 -t 120 app:app
