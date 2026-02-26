FROM python:3.11-slim-buster

WORKDIR /function

# 安装系统基础依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 复制并安装依赖
COPY requirements.txt /function/
RUN pip3 install --no-cache-dir -r requirements.txt

# 复制所有代码文件
COPY fetch_journals.py fetch_reports.py fetch_jobs.py func.py /function/

# 设置环境变量并运行 FDK
ENV PYTHONPATH=/function
ENTRYPOINT ["/usr/local/bin/fdk", "/function/func.py", "handler"]
