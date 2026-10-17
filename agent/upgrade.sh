#!/bin/bash
#========================================================
#   Description: 把已安装的 sss-agent 原地升级到低流量版
#   保留 service 里已有的 SERVER/USER/PASSWORD, 每台机器执行同一条命令即可:
#     curl -fsSL https://raw.githubusercontent.com/3198137738/ServerStatus/master/agent/upgrade.sh | sudo bash
#   可选: 覆盖默认间隔
#     curl -fsSL .../agent/upgrade.sh | sudo INTERVAL=10 PING_INTERVAL=0 bash
#   Github: https://github.com/3198137738/ServerStatus
#========================================================

GITHUB_RAW_URL="https://raw.githubusercontent.com/3198137738/ServerStatus/master"
SSS_AGENT_PATH="/opt/sss/agent"
SSS_AGENT_CLIENT="${SSS_AGENT_PATH}/client-linux.py"
SSS_AGENT_SERVICE="/etc/systemd/system/sss-agent.service"
INTERVAL="${INTERVAL:-5}"
PING_INTERVAL="${PING_INTERVAL:-30}"

red='\033[0;31m'
green='\033[0;32m'
plain='\033[0m'

fail() { echo -e "${red}[✗] $*${plain}"; exit 1; }
ok()   { echo -e "${green}[✓]${plain} $*"; }

[[ $EUID -ne 0 ]] && fail "必须使用 root 运行"
[ -f "$SSS_AGENT_SERVICE" ] || fail "未找到 $SSS_AGENT_SERVICE, 这台机器没装过 agent, 请用 sss-agent.sh 全新安装"
grep -q '^ExecStart=.*client-linux.py.*PASSWORD=' "$SSS_AGENT_SERVICE" || fail "$SSS_AGENT_SERVICE 的 ExecStart 格式不认识, 未做任何修改"
# 服务端 20s 收不到上报就断开连接
[[ "$INTERVAL" =~ ^[0-9]+$ ]] && [ "$INTERVAL" -ge 1 ] && [ "$INTERVAL" -le 15 ] || fail "INTERVAL 必须是 1~15"
[[ "$PING_INTERVAL" =~ ^[0-9]+$ ]] || fail "PING_INTERVAL 必须是非负整数"

# 1. 下载并校验新版
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
curl -fsSL --max-time 30 "${GITHUB_RAW_URL}/agent/client-linux.py" -o "$tmp" || fail "下载失败, 请检查本机能否访问 ${GITHUB_RAW_URL}"
grep -q 'PING_INTERVAL' "$tmp" || fail "下载到的不是低流量版(可能是 GitHub 缓存), 稍后重试"
python3 -c 'import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")' "$tmp" || fail "新版 client 语法校验失败"

# 2. 备份
mkdir -p "$SSS_AGENT_PATH"
[ -f "$SSS_AGENT_CLIENT" ] && cp -f "$SSS_AGENT_CLIENT" "${SSS_AGENT_CLIENT}.bak"
cp -f "$SSS_AGENT_SERVICE" "${SSS_AGENT_SERVICE}.bak"

rollback() {
    echo -e "${red}[✗] $*, 正在回滚${plain}"
    [ -f "${SSS_AGENT_CLIENT}.bak" ] && cp -f "${SSS_AGENT_CLIENT}.bak" "$SSS_AGENT_CLIENT"
    cp -f "${SSS_AGENT_SERVICE}.bak" "$SSS_AGENT_SERVICE"
    systemctl daemon-reload
    systemctl restart sss-agent
    exit 1
}

# 3. 替换 client, 改 ExecStart: 去掉已有的间隔参数再追加, 重复执行结果一致
install -m 644 "$tmp" "$SSS_AGENT_CLIENT"
sed -i -E '/^ExecStart=/{ s/ +(PING_INTERVAL|INTERVAL|ONLINE_CHECK_INTERVAL)=[^ ]*//g; s/$/ INTERVAL='"$INTERVAL"' PING_INTERVAL='"$PING_INTERVAL"'/ }' "$SSS_AGENT_SERVICE"

# 4. 重启并确认存活(连不上主控时 client 会循环重连而不是退出, 这里只能确认进程没崩)
systemctl daemon-reload
systemctl restart sss-agent || rollback "重启失败"
sleep $((INTERVAL + 3))
systemctl is-active --quiet sss-agent || rollback "agent 启动后退出"

ok "升级完成: INTERVAL=${INTERVAL}s PING_INTERVAL=${PING_INTERVAL}s"
grep '^ExecStart=' "$SSS_AGENT_SERVICE" | sed -E 's/PASSWORD=[^ ]*/PASSWORD=***/'
echo "到面板确认这台机器在线; 如需回滚:"
echo "  cp ${SSS_AGENT_CLIENT}.bak ${SSS_AGENT_CLIENT} && cp ${SSS_AGENT_SERVICE}.bak ${SSS_AGENT_SERVICE} && systemctl daemon-reload && systemctl restart sss-agent"
