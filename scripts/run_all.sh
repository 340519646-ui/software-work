#!/usr/bin/env bash
# 一键运行：fetch → extract → export → 生成成果看板
#
# 用法：
#   bash scripts/run_all.sh                        # 跑完整流程（用 config.yaml 里的分页范围）
#   bash scripts/run_all.sh --stages extract,export # 只跑指定阶段（不联网，秒级）
#   bash scripts/run_all.sh --pages 1 2            # 覆盖分页范围（采集第 1~2 页）
#   PY=python3 bash scripts/run_all.sh             # 换解释器
#
# 说明：本脚本只做编排与提示，真正的逻辑在 src/pipeline 里；失败即停并原样透出退出码。

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

PY="${PY:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "[FAIL] 找不到解释器 $PY"
  echo "       请先：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

STAGES="fetch,extract,export"
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --stages) STAGES="$2"; shift 2 ;;
    --pages)  EXTRA+=(--page-start "$2" --page-end "$3"); shift 3 ;;
    -h|--help)
      sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "[FAIL] 未知参数：$1"; exit 2 ;;
  esac
done

echo "解释器：$PY"
"$PY" -c "import sys; print('Python', sys.version.split()[0])"

IFS=',' read -ra LIST <<< "$STAGES"
for stage in "${LIST[@]}"; do
  echo
  echo "==================== 阶段：$stage ===================="
  started=$(date +%H:%M:%S)
  "$PY" -m src.pipeline.run --stage "$stage" "${EXTRA[@]}"
  code=$?
  echo "  退出码=$code（$started → $(date +%H:%M:%S)）"
  if [ "$code" != "0" ]; then
    echo
    echo "[STOP] 阶段 $stage 未成功（退出码 $code），已终止。"
    echo "       退出码含义：1=阶段有错误  2=参数错误  3=配置错误  4=登录态无效"
    exit "$code"
  fi
done

echo
echo "==================== 生成成果看板 ===================="
"$PY" scripts/make_report.py

echo
echo "全部完成。打开看板："
echo "  WSL 内：      xdg-open data/processed/report.html"
echo "  Windows 里：  \\\\wsl.localhost\\Ubuntu\\home\\user_01\\software-work\\data\\processed\\report.html"
