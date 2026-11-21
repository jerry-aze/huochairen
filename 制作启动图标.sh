#!/bin/bash
# 只需要运行一次：在这台 Mac 上生成可以双击启动的「火柴人.app」
# 用法：在终端里运行  bash 制作启动图标.sh
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/火柴人.app"
rm -rf "$APP"
osacompile -o "$APP" \
  -e 'set appPath to POSIX path of (path to me)' \
  -e 'do shell script "bash \"$(dirname " & quoted form of appPath & ")/launcher.sh\" > /dev/null 2>&1 &"' \
  || { echo "生成失败，请检查上面的报错"; exit 1; }
cp "$DIR/assets/AppIcon.icns" "$APP/Contents/Resources/applet.icns"
touch "$APP"
echo ""
echo "✅ 做好了！去这个文件夹双击「火柴人」即可启动（第一次会弹出终端安装依赖）。"
