#!/usr/bin/env bash
# WSL2 本地开发入口；配置由后端读取项目 .env，不在 Shell 中展开凭据。
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export PYTHONPATH="$PWD/backend"
export PATH="$PWD/.venv/bin:/usr/local/bin:/usr/bin:/bin"
python - <<'PY'
from app.config import get_settings
s=get_settings()
if s.adapter_mode != 'demo' or s.live_submit_enabled:
    raise SystemExit('本入口仅用于 demo 开发环境，要求 LR_ADAPTER_MODE=demo 且真实提交关闭。')
PY
if ss -ltnH 'sport = :18765' | grep -q .; then
    echo '18765 端口已占用，请先停止对应开发服务。' >&2
    exit 1
fi
mkdir -p runtime
python -m alembic -c backend/alembic.ini upgrade head
api_pid=''
worker_pid=''
cleanup() {
    trap - EXIT INT TERM
    for pid in "$api_pid" "$worker_pid"; do
        if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; fi
    done
    wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 18765 &
api_pid=$!
python -m app.worker &
worker_pid=$!
echo 'WSL2 开发服务：http://127.0.0.1:18765；Ctrl+C 停止 API 和 worker。'
wait -n "$api_pid" "$worker_pid"