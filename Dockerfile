# 用 Python 精简镜像（体积小，部署快）
FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 先复制依赖文件，利用 Docker 缓存加速
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

# 端口：Zeabur 默认 HTTP 8080，代码从环境变量 PORT 读取
# 这里不写死 PORT，让平台注入；只在代码缺省时 fallback 到 8080
ENV PORT=8080

# 启动网关
CMD ["python", "main.py"]
