#!/bin/bash
# 被「火柴人.app」调用的启动逻辑（由 制作启动图标.sh 生成的 app 调用）
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/运行日志.txt"
cd "$DIR" || exit 1
echo "==== $(date) 双击启动 ====" >> "$LOG"

if [ ! -f "$DIR/.venv/.installed" ]; then
  chmod +x "$DIR/启动.command" 2>/dev/null
  open -a Terminal "$DIR/启动.command"
  exit 0
fi

if pgrep -f "python.*pet\.py" >/dev/null; then
  osascript -e 'display notification "火柴人已经在运行了，看看屏幕右下角～" with title "火柴人"'
  exit 0
fi

nohup "$DIR/.venv/bin/python" "$DIR/pet.py" >> "$LOG" 2>&1 &
