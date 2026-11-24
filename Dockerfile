FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bms_tracker ./bms_tracker
# Mount config.yaml, .env and a data dir: docker run -v $PWD:/data ...
WORKDIR /data
CMD ["python", "-m", "bms_tracker", "run", "-c", "/data/config.yaml", "-s", "/data/state.json"]
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
