FROM python:3.11-slim
WORKDIR /function
RUN pip install --no-cache-dir fdk
COPY func.py .
ENTRYPOINT ["/usr/local/bin/fdk", "/function/func.py", "handler"]
