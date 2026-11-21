#!/bin/bash
# 双击运行：第一次会自动安装需要的东西（几分钟），之后秒开
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "这台 Mac 还没有 Python。马上会弹出安装窗口，装完后再双击我一次。"
  xcode-select --install
  read -p "按回车关闭…"
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "第一次运行，正在准备环境…"
  python3 -m venv .venv || { echo "创建环境失败"; read -p "按回车关闭…"; exit 1; }
fi
source .venv/bin/activate

if [ ! -f ".venv/.installed" ]; then
  echo "正在安装依赖（只有第一次需要）…"
  pip install -q --upgrade pip
  pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple \
    || pip install -q -r requirements.txt \
    || { echo "安装失败，请把上面的报错发给我"; read -p "按回车关闭…"; exit 1; }
  touch .venv/.installed
fi

echo "桌宠启动！这个窗口可以最小化，关掉它桌宠也会退出。"
python pet.py
