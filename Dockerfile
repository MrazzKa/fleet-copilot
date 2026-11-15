FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PORT=8000 HOST=0.0.0.0
COPY requirements.txt .
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" qrcode "websockets>=12"
COPY fleet ./fleet
COPY static ./static
COPY scenarios ./scenarios
COPY reports ./reports
COPY scripts ./scripts
RUN mkdir -p memory
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"
CMD ["python", "-m", "fleet.server"]
