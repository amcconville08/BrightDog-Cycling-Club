FROM python:3.12-slim
RUN groupadd -g 1001 club && useradd -u 1001 -g club -d /app -s /sbin/nologin club
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
RUN mkdir -p /data && chown club:club /data
USER club
EXPOSE 9206
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9206/health')" || exit 1
CMD ["gunicorn", "-w", "1", "--threads", "2", "-b", "0.0.0.0:9206", "--timeout", "120", "--access-logfile", "-", "app:app"]
