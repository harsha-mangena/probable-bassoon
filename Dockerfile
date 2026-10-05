FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080 CLINIC_DB=/data/clinic.db
WORKDIR /app
RUN groupadd --gid 10001 clinic && useradd --uid 10001 --gid clinic clinic && mkdir /data && chown clinic:clinic /data
COPY --chown=clinic:clinic engine.py auth.py call_flow.py telephony.py server.py manage.py clinic.toml index.html ./
COPY --chown=clinic:clinic web ./web
USER clinic
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=2)" || exit 1
CMD ["python", "server.py"]
