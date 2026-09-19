#!/usr/bin/env bash
set -euo pipefail
PACKAGE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "未找到 Python。请安装 Python 3.11+、venv 和 pip。" >&2
  exit 1
fi
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "需要 Python 3.11 或更新版本")'
if [[ ! -x "$PACKAGE_DIR/.venv/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$PACKAGE_DIR/.venv" || {
    echo "无法创建虚拟环境。Debian/Ubuntu 请安装匹配 Python 版本的 python3-venv。" >&2
    exit 1
  }
fi
shopt -s nullglob
wheels=("$PACKAGE_DIR"/clawmio-*-py3-none-any.whl)
if [[ ${#wheels[@]} -ne 1 ]]; then
  echo "发布包应包含且仅包含一个 ClawMio wheel，请重新解压完整发布包。" >&2
  exit 1
fi
"$PACKAGE_DIR/.venv/bin/python" -m pip install --upgrade "${wheels[0]}"
printf '\n安装完成。下一步：\n  %q start\n\n前台启动：\n  %q run\n\n配置和数据库默认保存在 ~/.clawmio。\n' "$PACKAGE_DIR/clawmio" "$PACKAGE_DIR/clawmio"
