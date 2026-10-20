#!/bin/bash
# 服务器上更新 ccim-hub（#19）。GitHub 部署钥匙在 authorized_keys 里被限定成只能跑这个脚本：
#   command="/home/ubuntu/ccim-hub/hub-update.sh",restrict ssh-ed25519 AAAA… ccim-deploy
# 源码打包（git archive 的 tar）从标准输入进来；SSH_ORIGINAL_COMMAND 里只认一个提交编号，记下来看是哪个版本。
# 新版本起不来就装回上一个。
set -euo pipefail
cd /home/ubuntu/ccim-hub

sha="${SSH_ORIGINAL_COMMAND:-}"
[[ "$sha" =~ ^[0-9a-f]{7,40}$ ]] || { echo "需要提交编号"; exit 2; }

exec 9>deploy.lock
flock -n 9 || { echo "另一次部署还没完"; exit 3; }

rm -rf src.new && mkdir src.new
head -c 50000000 | tar -x -C src.new
[ -f src.new/pyproject.toml ] && [ -f src.new/ccim/hubserver.py ] || { echo "包不对"; rm -rf src.new; exit 4; }

rm -rf src.prev
mv src src.prev
mv src.new src

install() { venv/bin/pip install -q --no-deps --force-reinstall "./$1" 2>&1 | tail -3; }
if install src && sudo systemctl restart ccim-hub && sleep 4 && systemctl is-active --quiet ccim-hub \
    && curl -fsk -o /dev/null https://127.0.0.1/ -H "Host: 150.158.91.156"; then
    echo "$sha $(date '+%F %T')" > deployed.txt
    echo "已部署 $sha"
else
    echo "新版本没起来，装回上一个"
    rm -rf src.bad && mv src src.bad && mv src.prev src
    install src; sudo systemctl restart ccim-hub
    exit 1
fi
