FROM python:3.11-slim

WORKDIR /function

# 直接安装依赖（fdk/gspread/google-auth 均为纯 Python，无需 build-essential）
COPY requirements.txt /function/
RUN pip3 install --no-cache-dir -r requirements.txt

# 复制所有代码文件
COPY fetch_journals.py fetch_reports.py fetch_jobs.py func.py /function/

# 设置环境变量并运行 FDK
ENV PYTHONPATH=/function
ENTRYPOINT ["/usr/local/bin/fdk", "/function/func.py", "handler"]
