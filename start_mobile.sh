#!/bin/bash
# stock-pi-mobile 启动脚本（树莓派 / 2.4寸 SPI 小屏）
# 用法：把本目录整体拷到 Pi，确保 stock_predict.py + stock_cache.db 同目录，
#       然后 chmod +x start_mobile.sh && ./start_mobile.sh
export FRAMEBUFFER=${FRAMEBUFFER:-/dev/fb1}
cd "$(dirname "$0")" || exit 1
LOG=/tmp/stock_mobile.log
while :; do
  python3 mobile_gui.py --fullscreen --nocursor >> "$LOG" 2>&1
  echo "[$(date '+%F %T')] exited rc=$?，3秒后重启" >> "$LOG"
  sleep 3
done
