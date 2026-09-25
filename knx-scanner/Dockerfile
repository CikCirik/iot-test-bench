# Interfata web pentru scanare/detectie manuala pe bus-ul KNX - doar citire
# (vezi app.py). Ruleaza cu network_mode: host in docker-compose.yaml,
# la fel ca nodered, necesar pentru multicast KNX/IP fiabil.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8003

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY static/ ./static/
COPY knx_manufacturers.json ./

EXPOSE 8003

CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
