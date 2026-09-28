# 烽火台 Beacon Tower · 全栈可观测运维监控平台 · 生产镜像
# 构建镜像不含密钥：SECRET_KEY / 接入令牌 / 数据库连接全部运行时注入
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖再拷代码：依赖未变时命中 Docker 层缓存
COPY requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt

COPY . .

# 非 root 运行（容器内只写 /data 卷：sqlite 兜底库与密钥文件）
RUN useradd --create-home obs \
    && mkdir -p /data \
    && chown -R obs:obs /app /data
USER obs

ENV DJANGO_DEBUG=0 \
    OBS_DATA_DIR=/data

EXPOSE 8014

# gunicorn：2 worker × 4 线程；本容器仅处理请求（OBS_DISABLE_WORKERS 由 compose 注入），
# 采集/告警/拨测/巡检线程由单副本 worker 服务（python manage.py obs_workers）承担
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:8014 --workers 2 --threads 4 --timeout 60 --access-logfile -"]
