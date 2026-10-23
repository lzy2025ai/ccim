#!/bin/sh
# ccim：把这台电脑加入总控（#175）。总控给出这个脚本时填好了下面几个值。
#   curl -fsSL <总控地址>/join.sh | sh -s <配对码>
# 先看这台电脑上的 ccim：没装就装（uv + Python + ccim，ccim 从总控下载），比总控旧就换成总控的版本，
# 一样就不动；然后加入总控、设成开机常驻。连不上 PyPI、GitHub 时依赖和 Python 走国内镜像。
# 环境变量：CCIM_MIRROR=1 一律走镜像，CCIM_MIRROR=0 一律不走。
set -u

HUB="@@HUB@@"
VERSION="@@VERSION@@"
COMMIT="@@COMMIT@@"
WHEEL="@@WHEEL@@"
SHA256="@@SHA256@@"
PY_VERSION=3.12
PYPI_MIRRORS="https://mirrors.aliyun.com/pypi/simple https://mirrors.cloud.tencent.com/pypi/simple https://pypi.tuna.tsinghua.edu.cn/simple"
PYTHON_MIRROR="https://registry.npmmirror.com/-/binary/python-build-standalone"
DIST="$HOME/.ccim/dist"

if [ -t 1 ]; then B="$(printf '\033[1m')"; R="$(printf '\033[31m')"; G="$(printf '\033[32m')"; N="$(printf '\033[0m')"; else B=; R=; G=; N=; fi
step() { printf '\n%s%s%s\n' "$B" "$*" "$N"; }
say() { printf '  %s\n' "$*"; }
report() {   # 告诉总控走到哪一步了，网页上「添加电脑」实时显示；失败不影响安装
  [ -n "$CODE" ] || return 0
  msg=$(printf '%s' "${2:-}" | tr -d '"\\' | tr '\n' ' ')
  curl -fsS -m 5 -o /dev/null -X POST -H 'Content-Type: application/json' \
    --data "{\"code\":\"$CODE\",\"step\":\"$1\",\"error\":\"$msg\"}" "$HUB/peer/progress" 2>/dev/null || true
}
fail() {     # fail 卡在哪 怎么办
  printf '\n%s✗ %s%s\n' "$R" "$1" "$N"
  [ -n "${2:-}" ] && printf '  %s\n' "$2"
  report failed "$1"
  exit 1
}
reach() { curl -fsS -o /dev/null -m "$2" "$1" 2>/dev/null; }
sha256() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1; else sha256sum "$1" | cut -d' ' -f1; fi; }

# 整个放在函数里：curl | sh 时 sh 边读边执行，先读完再跑，里面的命令也就读不到后面的脚本
main() {
CODE="${1:-}"

case "$CODE" in
  ""|[0-9][0-9][0-9][0-9][0-9][0-9]) ;;
  *) fail "配对码不对：$CODE" "配对码是 6 位数字，在网页「设置 → 电脑 → 添加电脑」里生成。" ;;
esac
command -v curl >/dev/null 2>&1 || fail "这台电脑上没有 curl" "先装好 curl 再运行。"
case "$(uname -s)" in
  Darwin) OS=mac ;;
  Linux) OS=linux ;;
  *) fail "不支持这个系统：$(uname -s)" "目前只支持 macOS 和 Linux。" ;;
esac
report started

printf '%s把这台电脑加入 ccim 总控%s %s\n' "$B" "$N" "$HUB"

# ---------- 1. 这台电脑上的 ccim ----------
step "1/4 检查这台电脑上的 ccim"
CCIM=""
for c in "$(command -v ccim 2>/dev/null || true)" "$HOME/.local/bin/ccim"; do
  if [ -n "$c" ] && [ -x "$c" ]; then CCIM="$c"; break; fi
done
KIND=""; HAVE=""; HAVE_VER=""
if [ -n "$CCIM" ]; then
  PY=""
  line=$(head -n 1 "$CCIM" 2>/dev/null || true)
  case "$line" in
    "#!"*python*) PY=${line#??}; PY=${PY%% *} ;;
    "#!/bin/sh"*) PY=$(sed -n "2s/^'''exec' '\{0,1\}\"\{0,1\}\([^\"' ]*python[^\"' ]*\).*/\1/p" "$CCIM") ;;
  esac
  out=""
  if [ -n "$PY" ] && [ -x "$PY" ]; then
    out=$("$PY" -c 'import sys;sys.path=[p for p in sys.path if p];from ccim import upgrade;i=upgrade.info();print(i.get("kind") or "-",i.get("commit") or "-",i.get("version") or "-")' 2>/dev/null || true)
  fi
  KIND=$(echo "$out" | cut -d' ' -f1); HAVE=$(echo "$out" | cut -d' ' -f2); HAVE_VER=$(echo "$out" | cut -d' ' -f3)
  [ -n "$KIND" ] || KIND=old
  [ "$HAVE" = "-" ] && HAVE=""
fi

JOINED=""
if [ -f "$HOME/.ccim/hub.json" ]; then
  JOINED=$(sed -n 's/^ *"url": *"\([^"]*\)".*/\1/p' "$HOME/.ccim/hub.json" | head -n 1)
fi

ACTION=install
if [ -z "$CCIM" ]; then
  say "还没装 ccim，接下来装 $VERSION"
elif [ "$KIND" = dev ] || [ "$KIND" = stable ]; then
  ACTION=keep
  say "这台电脑跑的是开发用的 ccim（${CCIM}），不改动它"
elif [ -n "$COMMIT" ] && [ "$HAVE" = "$COMMIT" ]; then
  ACTION=keep
  say "已经装了 ccim ${HAVE_VER}，和总控的版本一样"
else
  ACTION=upgrade
  say "已经装了 ccim ${HAVE_VER:-（版本较旧）}，接下来换成总控的版本 $VERSION"
fi

if [ -n "$JOINED" ] && [ "$JOINED" = "$HUB" ]; then
  say "已经加入这个总控"
elif [ -n "$JOINED" ]; then
  printf '\n  这台电脑已经加入了另一个总控：%s\n' "$JOINED"
  ans=""; tty=""
  if (exec </dev/tty) 2>/dev/null; then
    tty=1
    printf '  改成加入 %s 吗？原来那边就看不到这台电脑了，这台电脑上的 Agent 和对话不受影响。(y/N) ' "$HUB"
    read -r ans </dev/tty || ans=""
  fi
  case "$ans" in
    y|Y|yes|是) [ -n "$CODE" ] || fail "缺配对码" "在网页「设置 → 电脑 → 添加电脑」里生成配对码，再运行网页上给的命令。" ;;
    *) if [ -n "$tty" ]; then hint="要换过来，再运行一次这条命令并回答 y。"
       else hint="要换过来，先运行 ccim join --leave，再运行一次这条命令。"; fi
       fail "这台电脑已经加入了别的总控，没有改动" "$hint" ;;
  esac
elif [ -z "$CODE" ]; then
  fail "缺配对码" "在网页「设置 → 电脑 → 添加电脑」里生成配对码，再运行网页上给的命令。"
fi

# ---------- 2. 网络 ----------
UV=""
for c in "$(command -v uv 2>/dev/null || true)" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
  if [ -n "$c" ] && [ -x "$c" ]; then UV="$c"; break; fi
done

if [ "$ACTION" != keep ]; then
  step "2/4 检查网络"
  MIRROR=""; GH_SLOW=""
  case "${CCIM_MIRROR:-}" in
    1) GH_SLOW=1; MIRROR=pick ;;
    0) ;;
    *)
      reach https://pypi.org/simple/six/ 4 || MIRROR=pick
      reach https://github.com/ 4 || GH_SLOW=1 ;;
  esac
  if [ "$MIRROR" = pick ]; then
    MIRROR=""
    for m in $PYPI_MIRRORS; do
      if reach "$m/six/" 4; then MIRROR="$m"; break; fi
    done
    [ -n "$MIRROR" ] || fail "连不上 PyPI，也连不上国内的 PyPI 镜像" "检查这台电脑能不能上网，再运行一次。"
    say "PyPI 连不上或太慢，Python 依赖从镜像下载：$MIRROR"
    export UV_DEFAULT_INDEX="$MIRROR" UV_INDEX_URL="$MIRROR"
  else
    say "PyPI 能连上"
  fi
  if [ -n "$GH_SLOW" ]; then
    say "GitHub 连不上或太慢，Python 从镜像下载"
    export UV_PYTHON_INSTALL_MIRROR="$PYTHON_MIRROR"
  else
    say "GitHub 能连上"
  fi

  # ---------- 3. 安装 ----------
  step "3/4 安装 ccim $VERSION"
  report installing
  if [ -z "$UV" ]; then
    say "安装 uv（Python 的安装工具）"
    mkdir -p "$HOME/.local/bin"
    if [ -z "$GH_SLOW" ] && curl -fsSL -m 120 https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null 2>&1; then
      :
    else
      # 从 PyPI（镜像）拿 uv 的 wheel，里面就是 uv 程序
      case "$OS-$(uname -m)" in
        mac-arm64) TAG=macosx_11_0_arm64 ;;
        mac-x86_64) TAG=macosx_10_12_x86_64 ;;
        linux-x86_64) TAG=manylinux_2_17_x86_64 ;;
        linux-aarch64|linux-arm64) TAG=manylinux_2_17_aarch64 ;;
        *) fail "不认识这台电脑的处理器：$(uname -m)" "先自己装好 uv（https://docs.astral.sh/uv/），再运行一次。" ;;
      esac
      idx="${MIRROR:-https://pypi.org/simple}"
      href=$(curl -fsSL -m 30 "$idx/uv/" | grep -o "href=\"[^\"]*uv-[0-9][0-9.]*-py3-none-[^\"]*$TAG[^\"]*\.whl[^\"]*\"" |
        sed 's/^href="//; s/"$//' | awk '{
          f=$0; sub(/.*\/uv-/, "", f); split(f, a, "-"); n=split(a[1], v, ".");
          k=sprintf("%06d%06d%06d", v[1], v[2], v[3]); if (k > best) { best=k; url=$0 } } END { print url }')
      [ -n "$href" ] || fail "下载 uv 失败：在 $idx 里没找到" "检查网络，再运行一次；或者先自己装好 uv。"
      case "$href" in
        http*) url="$href" ;;
        /*) url="$(echo "$idx" | sed 's#^\(https*://[^/]*\).*#\1#')$href" ;;
        ../../*) url="${idx%/simple}/${href#../../}" ;;
        *) url="$idx/uv/$href" ;;
      esac
      want=$(echo "$url" | sed -n 's/.*#sha256=\([0-9a-f]*\).*/\1/p'); url=${url%%#*}
      tmp=$(mktemp -d)
      curl -fsSL --retry 2 -m 600 -o "$tmp/uv.whl" "$url" || fail "下载 uv 失败" "检查网络，再运行一次。"
      [ -z "$want" ] || [ "$(sha256 "$tmp/uv.whl")" = "$want" ] || fail "下载的 uv 不完整" "再运行一次。"
      if command -v unzip >/dev/null 2>&1; then
        unzip -q -o -j "$tmp/uv.whl" '*.data/scripts/uv' -d "$HOME/.local/bin" || fail "解压 uv 失败" "再运行一次。"
      elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import sys,zipfile,shutil;z=zipfile.ZipFile(sys.argv[1]);n=[x for x in z.namelist() if x.endswith(".data/scripts/uv")][0];shutil.copyfileobj(z.open(n),open(sys.argv[2],"wb"))' \
          "$tmp/uv.whl" "$HOME/.local/bin/uv" || fail "解压 uv 失败" "再运行一次。"
      else
        fail "解压 uv 要用 unzip" "先装好 unzip（比如 sudo apt install unzip），再运行一次。"
      fi
      chmod +x "$HOME/.local/bin/uv"
      rm -rf "$tmp"
    fi
    UV="$HOME/.local/bin/uv"
    [ -x "$UV" ] || fail "安装 uv 失败" "检查网络，再运行一次；或者先自己装好 uv（https://docs.astral.sh/uv/）。"
    say "uv 装好了"
  fi

  say "从总控下载 ccim"
  mkdir -p "$DIST"
  curl -fL --retry 2 -m 300 -sS -o "$DIST/$WHEEL.part" "$HUB/peer/dl/$WHEEL" \
    || fail "从总控下载 ccim 失败" "确认这台电脑能打开 ${HUB}，再运行一次。"
  [ "$(sha256 "$DIST/$WHEEL.part")" = "$SHA256" ] || fail "下载的 ccim 不完整" "再运行一次。"
  mv "$DIST/$WHEEL.part" "$DIST/$WHEEL"

  say "安装 ccim 和它用到的 Python ${PY_VERSION}、依赖（第一次要几分钟）"
  # 只用编译好的包：没有现成安装包的新版本（比如 cryptography 新版不再出 Intel Mac 的）自动退到有的那一版，不在本机编译
  UV_PYTHON_PREFERENCE=only-managed "$UV" tool install --force --compile-bytecode --no-build --python "$PY_VERSION" "$DIST/$WHEEL" \
    || UV_PYTHON_PREFERENCE=only-managed "$UV" tool install --force --compile-bytecode --python "$PY_VERSION" "$DIST/$WHEEL" \
    || fail "安装 ccim 失败（上面是出错的地方）" "多半是网络不稳，再运行一次；还不行的话加上 CCIM_MIRROR=1 走国内镜像：curl -fsSL $HUB/join.sh | CCIM_MIRROR=1 sh -s $CODE"
  CCIM="$("$UV" tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")/ccim"
  [ -x "$CCIM" ] || fail "装好后没找到 ccim 命令" "再运行一次。"
  say "ccim $VERSION 装好了"
else
  step "2/4 检查网络"
  say "不用安装，跳过"
  step "3/4 安装 ccim"
  say "跳过"
fi

# ---------- 4. 加入总控、开机常驻 ----------
step "4/4 加入总控"
report joining
set -- "$HUB"
[ -n "$CODE" ] && set -- "$@" "$CODE"
[ "$ACTION" = upgrade ] && set -- "$@" --restart
"$CCIM" join "$@" || fail "加入总控失败（原因见上面）" "按上面说的处理后，再运行一次。"

if ! command -v ccim >/dev/null 2>&1 && [ -n "$UV" ]; then
  "$UV" tool update-shell >/dev/null 2>&1 || true
  say "新开一个终端窗口就能用 ccim 命令"
fi
printf '\n%s✓ 好了%s 这台电脑已经加入总控，在网页「设置 → 电脑」里能看到它。\n' "$G" "$N"
}

main "$@" </dev/null
