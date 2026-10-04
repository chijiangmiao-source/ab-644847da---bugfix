FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    AUDIT_DATA_DIR=/data

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY scripts ./scripts

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=3s --retries=5 \
    CMD python -c "import json,os,urllib.request,sys; sys.exit(0 if json.load(urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080')))['status']=='ok' else 1)"

CMD ["python", "-m", "app.main"]
