#!/bin/bash
# macOS / Linux 一键安装运行
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "没找到 python3，请先装 Python 3.11/3.12"
  exit 1
fi
if [ ! -d ".venv" ]; then
  echo "[1/3] 创建虚拟环境..."
  python3 -m venv .venv
fi
echo "[2/3] 安装依赖..."
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "没找到 ffmpeg，请先装：macOS 用 brew install ffmpeg；Ubuntu 用 sudo apt install ffmpeg"
  exit 1
fi
echo "[3/3] 自测..."
.venv/bin/python yt_subtitle.py --self-test
echo "打开图形界面..."
.venv/bin/python yt_subtitle.py
