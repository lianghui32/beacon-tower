#!/usr/bin/env bash
# ============================================================================
# start.sh — 烽火台 Beacon Tower · 全栈可观测运维监控平台 · 一键启动脚本
#
# 一条命令从零到能跑，两种模式：
#
#   bash start.sh               本地模式（默认，无需 Docker）：
#                               检测 Python -> 建虚拟环境 -> 装依赖 -> 建库迁移
#                               -> 首次运行自动建管理员/演示数据 -> 起服务 8014
#
#   bash start.sh --docker      生产编排模式：
#                               检查 Docker/Compose（缺失给出对应平台的安装指引）
#                               -> 自动生成 .env 密钥 -> docker compose up --build
#                               -> 等健康检查通过
#
#   bash start.sh --setup-only  只做环境准备（本地模式的前 5 步），不启动服务
#   bash start.sh --demo        本地模式下强制初始化演示数据（全新库会自动执行）
#
# 脚本幂等：重复运行安全（已有 venv/依赖/账号自动跳过）。
# 安全约定：脚本不下载并执行任何远程脚本；Docker 缺失时只打印官方安装命令。
# ============================================================================
set -uo pipefail

PORT=8014
VENV_DIR=".venv"
ENV_FILE=".env"
CRED_FILE="admin_credentials.txt"

say()  { printf '%s\n' "$*"; }
warn() { printf '[!] %s\n' "$*"; }
die()  { warn "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Python 探测：跳过 Windows 商店的假 python 别名，要求 >= 3.10
# ---------------------------------------------------------------------------
PY_CMD=""
detect_python() {
  for cand in "python" "python3" "py -3"; do
    # shellcheck disable=SC2086
    if $cand -c 'import sys; assert sys.version_info >= (3, 10)' >/dev/null 2>&1; then
      PY_CMD="$cand"
      return 0
    fi
  done
  return 1
}

# ---------------------------------------------------------------------------
# Docker 编排模式：检查 -> 密钥 -> 构建 -> 健康检查
# ---------------------------------------------------------------------------
docker_deploy() {
  say "== 生产编排模式（Docker Compose）=="

  if ! command -v docker >/dev/null 2>&1; then
    case "$(uname -s)" in
      Linux)
        warn "未检测到 Docker。请先安装（官方一键脚本，需要 root/sudo，建议先审查脚本内容）："
        say "    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && sh /tmp/get-docker.sh"
        say "安装完成并启动 dockerd 后，重新运行: bash start.sh --docker"
        ;;
      Darwin)
        warn "未检测到 Docker。请先安装并启动 Docker Desktop："
        say "    brew install --cask docker   # 或从 https://www.docker.com/products/docker-desktop/ 下载"
        ;;
      *)
        warn "未检测到 Docker。Windows 安装 Docker Desktop（需要 WSL2，装完需重启）："
        say "    winget install Docker.DockerDesktop"
        say "    # 或从 https://www.docker.com/products/docker-desktop/ 下载"
        say "不想装 Docker？本地模式即可运行：bash start.sh"
        ;;
    esac
    exit 1
  fi

  docker info >/dev/null 2>&1 \
    || die "Docker 已安装但守护进程未运行，请先启动 Docker Desktop / dockerd"

  if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
  else
    die "缺少 Docker Compose 插件：docker compose version 不可用"
  fi
  say "Docker 就绪：$(docker --version)"

  if [ ! -f "$ENV_FILE" ]; then
    # 密钥生成：优先 openssl，其次 python；两者都没有则拒绝（不使用裸管道读随机源）
    if command -v openssl >/dev/null 2>&1; then
      SECRET_KEY=$(openssl rand -hex 32)
      INGEST_TOKEN=$(openssl rand -hex 24)
      PG_PASSWORD=$(openssl rand -hex 16)
    elif detect_python; then
      # shellcheck disable=SC2086
      SECRET_KEY=$($PY_CMD -c 'import secrets; print(secrets.token_urlsafe(48))')
      INGEST_TOKEN=$($PY_CMD -c 'import secrets; print(secrets.token_urlsafe(24))')
      PG_PASSWORD=$($PY_CMD -c 'import secrets; print(secrets.token_urlsafe(16))')
    else
      die "生成密钥需要 openssl 或 python 之一，均未找到"
    fi
    say "生成 $ENV_FILE（含随机密钥，请妥善保管，勿提交仓库）..."
    {
      echo "DJANGO_SECRET_KEY=$SECRET_KEY"
      echo "OBS_INGEST_TOKEN=$INGEST_TOKEN"
      echo "OBS_PG_PASSWORD=$PG_PASSWORD"
      echo "DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1"
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE" 2>/dev/null || true
  else
    say "$ENV_FILE 已存在，沿用其中密钥"
  fi

  say "构建并启动（首次构建需下载依赖镜像，几分钟）..."
  $COMPOSE up -d --build || die "docker compose 启动失败"

  say "等待健康检查（最多 60 秒）..."
  ok=0
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$PORT/api/health/" >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 2
  done

  if [ "$ok" = "1" ]; then
    say ""
    say "============================================================"
    say "  平台已运行：http://127.0.0.1:$PORT"
    say "  创建管理员：$COMPOSE exec web python manage.py createsuperuser"
    say "  演示数据：  $COMPOSE exec web python manage.py init_data"
    say "  接入令牌：  $ENV_FILE 里的 OBS_INGEST_TOKEN"
    say "  常用命令：  $COMPOSE logs -f web  |  $COMPOSE down"
    say "============================================================"
  else
    warn "健康检查超时。查看日志：$COMPOSE logs web"
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# 本地模式：venv + 依赖 + 迁移 + 账号 + runserver
# ---------------------------------------------------------------------------
local_run() {
  detect_python \
    || die "未找到 Python >= 3.10。请先安装：https://www.python.org/downloads/（Windows 安装时勾选 Add to PATH）"
  say "[1/5] Python 就绪：$PY_CMD"

  if [ "$(uname -s)" = "Linux" ] || [ "$(uname -s)" = "Darwin" ]; then
    VENV_PY="$VENV_DIR/bin/python"
  else
    VENV_PY="$VENV_DIR/Scripts/python.exe"   # Windows
  fi

  if [ -x "$VENV_PY" ] || [ -f "$VENV_PY" ]; then
    say "[2/5] 虚拟环境已存在（$VENV_DIR）"
  else
    say "[2/5] 创建虚拟环境 $VENV_DIR ..."
    $PY_CMD -m venv "$VENV_DIR" || die "venv 创建失败"
  fi

  say "      安装依赖（首次较慢）..."
  "$VENV_PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt \
    || die "依赖安装失败，请检查网络后重试"
  PY="$VENV_PY"   # 之后统一用虚拟环境里的 Python

  say "[3/5] 数据库迁移..."
  "$PY" manage.py migrate --noinput >/dev/null \
    || die "迁移失败（可手动运行 $PY manage.py migrate 查看报错）"

  say "[4/5] 检查账号与数据..."
  USER_COUNT=$("$PY" manage.py shell -c \
    'from django.contrib.auth.models import User; print(User.objects.count())' 2>/dev/null \
    | tail -1)

  if [ "${USER_COUNT:-0}" = "0" ] || [ "${1:-}" = "--demo" ]; then
    say "      初始化演示数据（论坛 + 24 小时观测数据，稍等）..."
    "$PY" manage.py init_data >/dev/null || warn "演示数据初始化失败（不影响平台运行）"
  fi

  SUPER_EXISTS=$("$PY" manage.py shell -c \
    'from django.contrib.auth.models import User; print(1 if User.objects.filter(is_superuser=True).exists() else 0)' 2>/dev/null \
    | tail -1)

  if [ "$SUPER_EXISTS" = "0" ]; then
    ADMIN_PASS=$("$PY" -c 'import secrets; print(secrets.token_urlsafe(12))')
    "$PY" manage.py shell -c "
from django.contrib.auth.models import User
User.objects.create_superuser('admin', '', '$ADMIN_PASS')
" >/dev/null || die "管理员创建失败"
    printf '平台管理员（建议登录后修改密码）\nusername=admin\npassword=%s\n' \
      "$ADMIN_PASS" > "$CRED_FILE"
    say "      已创建管理员 admin，凭据写入 $CRED_FILE"
  else
    say "      管理员已存在，跳过（忘记密码: $PY manage.py changepassword admin）"
  fi

  say "[5/5] 端口 $PORT 检查..."
  if "$PY" - <<'EOF' 2>/dev/null
import socket, sys
s = socket.socket()
try:
    s.bind(("0.0.0.0", 8014))
except OSError:
    sys.exit(1)
finally:
    s.close()
EOF
  then
    say "      端口 $PORT 空闲"
  else
    if [ "${1:-}" = "--setup-only" ]; then
      warn "端口 $PORT 被占用（--setup-only 模式不启动服务，可忽略）"
    else
      die "端口 $PORT 已被占用：平台可能已在运行（试试打开 http://127.0.0.1:$PORT）；或用 netstat -ano | findstr $PORT 找到进程结束它"
    fi
  fi

  if [ "${1:-}" = "--setup-only" ]; then
    say "环境准备完成。启动：bash start.sh"
    return 0
  fi

  say ""
  say "============================================================"
  say "  平台启动中：http://127.0.0.1:$PORT   （局域网访问 http://<本机IP>:$PORT）"
  say "  登录账号：管理员见 $CRED_FILE；只读演示账号：$PY manage.py create_demo_account"
  say "  Agent 接入令牌：项目根目录 ingest_token.txt（接入中心页面有完整示例）"
  say "  Ctrl+C 停止服务"
  say "============================================================"
  say ""
  exec "$PY" manage.py runserver "0.0.0.0:$PORT"
}

case "${1:-}" in
  --docker)          docker_deploy ;;
  --setup-only)      local_run "$@" ;;
  --demo)            local_run "$@" ;;
  "")                local_run ;;
  --help|-h)
    say "用法: bash start.sh [--docker | --setup-only | --demo]"
    ;;
  *)
    die "未知参数 $1。用法: bash start.sh [--docker | --setup-only | --demo]"
    ;;
esac
