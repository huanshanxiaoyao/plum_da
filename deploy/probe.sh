#!/usr/bin/env bash
# 机器 B（数仓机）部署前的只读体检，对应 deploy/README.md 的第 0 步。
#
#     bash deploy/probe.sh
#
# 机器 B 不是空机：它同时是主库的异地备份落地机，可能已有 PostgreSQL 实例、
# 已有的 systemd 单元和已有账号。这个脚本只探测不动手，输出用来填 README 后续
# 步骤里的四个变量：包管理器、PG 端口、是否已有 16 集群、是否已有 plum_da 账号。
#
# 退出码恒为 0：这是体检不是门禁。

set -uo pipefail

ok()   { printf '[ok] %s\n' "$*"; }
bad()  { printf '[!!] %s\n' "$*"; }
info() { printf '     %s\n' "$*"; }

echo "== 1. 系统 =="
. /etc/os-release 2>/dev/null && ok "$PRETTY_NAME"
case "${ID_LIKE:-$ID}" in
    *debian*|*ubuntu*) info "包管理器：apt-get，PG 包名 postgresql-16" ;;
    *rhel*|*fedora*)   info "包管理器：dnf，PG 包名 postgresql16-server（PGDG 源，装完要 initdb）" ;;
    *) bad "发行版不在预期内，README 第 1 步的包名要自己换" ;;
esac
info "架构：$(uname -m)"

echo
echo "== 2. Python 与 rsync =="
PY=$(python3 --version 2>&1)
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null \
    && ok "$PY（≥ 3.11）" || bad "$PY —— 需要 ≥ 3.11"
python3 -c 'import venv' 2>/dev/null && ok "venv 可用" || bad "缺 python3-venv"
command -v rsync >/dev/null && ok "rsync $(rsync --version | head -1 | awk '{print $3}')" || bad "缺 rsync"

echo
echo "== 3. PostgreSQL =="
if command -v pg_lsclusters >/dev/null; then
    pg_lsclusters | sed 's/^/     /'
elif sudo -n -u postgres psql -tAc 'select version()' >/dev/null 2>&1; then
    info "$(sudo -n -u postgres psql -tAc 'select version()' | cut -c1-60)"
else
    bad "没有发现可用集群——README 第 1 步要真装一个"
fi
PORT=$(sudo -n -u postgres psql -tAc 'show port' 2>/dev/null | tr -d ' ')
if [ -n "$PORT" ]; then
    ok "已有集群端口 = $PORT"
    # 机器 A 的主库在 55432，5432 只是默认值不是事实，README 后面每处都要填这个真实值。
    [ "$PORT" = "5432" ] || info "注意：不是 5432，/etc/plum_da.env 的 DSN 要写 $PORT"
else
    info "拿不到端口（可能没有集群，或当前账号不能 sudo -u postgres）"
fi
VER=$(sudo -n -u postgres psql -tAc 'show server_version' 2>/dev/null | cut -d. -f1)
[ -n "$VER" ] && { [ "$VER" = "16" ] && ok "大版本 16，可直接复用" || bad "大版本 $VER ≠ 16，不要复用，另起 16 集群"; }
info "PG 相关单元名（写进 plum-da-ingest.service 的 After=）："
systemctl list-units --type=service --all 2>/dev/null | grep -i postgres | awk '{print "       " $1}' || info "       （无）"
psql -tAc "select 1 from pg_database where datname='plum_da'" >/dev/null 2>&1 && bad "库 plum_da 已存在——bootstrap 是幂等的，但先确认里面有没有数据"

echo
echo "== 4. 账号与目录 =="
getent passwd plum_da >/dev/null && ok "系统账号 plum_da 已存在（README 第 2 步可跳过）" \
                                 || bad "系统账号 plum_da 不存在（服务以它运行，必须先建）"
[ -e /opt/plum_da ]     && info "/opt/plum_da 已存在：$(ls -ld /opt/plum_da)"     || info "/opt/plum_da 未创建（service 单元写死了这个路径）"
[ -e /srv/plum_landing ] && info "/srv/plum_landing 已存在：$(ls -ld /srv/plum_landing)" || info "/srv/plum_landing 未创建"
[ -e /etc/plum_da.env ]  && info "/etc/plum_da.env 已存在：$(ls -l /etc/plum_da.env)"    || info "/etc/plum_da.env 未创建"
[ -e /home/plum_da/.ssh/plum_da_pull ] && ok "拉取私钥已就位" || info "拉取私钥未生成（README 第 4 步）"
sudo -n test -f /home/plum_da/.ssh/known_hosts 2>/dev/null \
    && ok "known_hosts 已预置" \
    || bad "known_hosts 未预置——拉取端写死 StrictHostKeyChecking=yes，首连必失败且静默"

echo
echo "体检结束。把上面的输出贴回去，README 里的 <占位符> 就能全部填成具体值。"
