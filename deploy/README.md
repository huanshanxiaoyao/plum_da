# 机器 B 部署步骤

2026-09-09 线上交接已确认：一期核心业务数据链路验收通过，分析模型、只读看板和
Watchdog 已部署，受控通知已由用户确认收到。四项运维待验收及脱敏证据见
[Analytics V1 验收记录](../docs/analytics-v1-acceptance.md)。安装与回滚参考见
[Analytics V1](analytics-v1.md)。本轮文档收尾不重复初始化、不部署或重启服务。

以下是首次安装参考步骤，现有生产环境已经完成相应安装。未来安装时按顺序执行，
每一步都有自查命令，**自查不通过不要进下一步**——
这条链路的失败模式大多是「静默地什么都没发生」，靠自查才看得见。

> ⚠️ **机器 B 不是空机**：它同时是主库的异地备份落地机，上面很可能已经有一个
> PostgreSQL 实例、已有的 systemd 单元和已有的账号。下面每一步都先探测再动手，
> 不要照抄「安装 / 建库」的写法去覆盖既有集群。

## 0. 先探测，再决定

```bash
bash deploy/probe.sh
```

只读，不动任何东西。输出用来填后面每一步的四个变量：**包管理器**、**PG 端口**、
**是否已有 16 集群**、**是否已有 `plum_da` 账号**，外加 `plum-da-ingest.service`
的 `After=` 该写哪个单元名。

对侧（机器 A）有一份对应的体检脚本，在上游仓库里：

```bash
bash scripts/probe_analytics_node.sh    # 在机器 A 上跑
```

它把「机器 B 拉到 0 个文件」的各种静默失因一次摊开——落盘目录、拉取账号、
`authorized_keys` 限制、目录可读性。**两边都跑完再开始装**，
否则第 6 步的 `--dry-run` 报 0 个文件时你分不清是哪一侧的问题。

## 1. 依赖

只在第 0 步显示缺失时才装。发行版包名不同：

```bash
# Debian / Ubuntu
sudo apt-get install -y postgresql-16 rsync
# RHEL 系（PGDG 仓库）
sudo dnf install -y postgresql16-server rsync && sudo /usr/pgsql-16/bin/postgresql-16-setup initdb
sudo systemctl enable --now postgresql-16   # 单元名随发行版：postgresql / postgresql@16-main / postgresql-16
```

PG 大版本必须是 **16**，与机器 A 及国内库保持一致（见上游 `AGENTS.md` 的版本表）。
这里是独立的分析库，与主业务库、与备份落地的实例都无关——如果第 0 步发现已有集群
且大版本就是 16，**直接复用它，不要再装一个**，只需记住它的真实端口。

`plum-da-ingest.service` 的 `After=` 默认写的是 Debian/Ubuntu 的 `postgresql.service`。
RHEL 系是 `postgresql-16.service`，**不一样就改 service 文件**——`After=` 指向不存在
的单元不会报错，只是那句依赖静默失效。第 0 步的体检会打印本机的真实单元名。

## 2. 系统账号

服务以 `User=plum_da` 运行，落地目录和 SSH 私钥也归它。**这是操作系统账号，
和第 3 步的 PostgreSQL 角色同名但是两回事**，缺了它第 4 步的 `install -o plum_da`
会直接失败。

```bash
sudo useradd --system --create-home --home-dir /home/plum_da --shell /usr/sbin/nologin plum_da
```

`--shell` 用 nologin：这个账号只被 systemd 拉起，不需要交互登录。
（RHEL 系路径是 `/sbin/nologin`。）

自查：

```bash
getent passwd plum_da && sudo test -d /home/plum_da && echo ok
```

## 3. 建库与授权

先把仓库放到 `/opt/plum_da`——service 单元里的 `WorkingDirectory` 和 `ExecStart`
都写死了这个路径，别在别处 clone 再想着改单元。

```bash
sudo git clone <repo> /opt/plum_da
sudo chown -R plum_da:plum_da /opt/plum_da
```

```bash
sudo -u postgres psql -p <第0步的端口> \
  -v role_password="<从密码管理器取>" -f /opt/plum_da/deploy/bootstrap_db.sql
```

⚠️ 两个坑：

- `sudo -u postgres` 之后**当前目录必须是 `postgres` 能进的**（`/opt/plum_da` 可以，
  你的家目录多半不行）。报 `Permission denied` 时先 `cd /`。
- 这个脚本**不可重复执行**：`CREATE ROLE` / `CREATE DATABASE` 在第二次会报
  `already exists` 并中止。中途失败重来之前，先确认要不要清掉半成品：
  `DROP DATABASE IF EXISTS plum_da; DROP ROLE IF EXISTS plum_da;`
  ——**只在确认这库里没有已装载数据时才做**。

两行自查都必须是 `t`。

## 4. 打通 SSH（B 拉 A，单向）

在机器 B 上**以 `plum_da` 身份**生成密钥——私钥路径必须与第 5 步的
`PLUM_DA_SSH_KEY` 一致，而服务是以 `plum_da` 跑的，密钥生成在你自己的家目录里
它读不到：

```bash
sudo -u plum_da mkdir -p /home/plum_da/.ssh
sudo -u plum_da chmod 700 /home/plum_da/.ssh
sudo -u plum_da ssh-keygen -t ed25519 -f /home/plum_da/.ssh/plum_da_pull -N ''
sudo cat /home/plum_da/.ssh/plum_da_pull.pub    # 贴到机器 A
```

机器 A 那侧需要一个**只读的受限账号**（见下面「机器 A 前置」），
`authorized_keys` 里用 `command=` 限制成只能跑 rsync。

方向是 **B 主动拉 A**。注意机器 A 已经因为异地备份（INFRA-8）持有一把指向 B 的
私钥，所以「A 完全不碰 B」并不成立；但那是备份推送的既有链路，
埋点这条**不要再给 A 增加指向 B 的凭据**，A 是线上业务机。

还要**预置机器 A 的 host key**。拉取端写死了
`StrictHostKeyChecking=yes`（`ingest/pull.py`），首次连接没有 known_hosts 会直接失败，
而且是在 systemd 里静默失败——不会有交互式的「是否信任」提示可看：

```bash
sudo -u plum_da bash -c 'ssh-keyscan -H <机器A> >> /home/plum_da/.ssh/known_hosts'
```

自查（用 `plum_da` 的身份跑，不是你自己的；`BatchMode=yes` 才和真实运行一致）：

```bash
sudo -u plum_da ssh -i /home/plum_da/.ssh/plum_da_pull \
  -o BatchMode=yes -o StrictHostKeyChecking=yes \
  <user>@<机器A> 'ls /var/lib/plum/analytics | head'
```

### 机器 A 前置（不在本机执行）

机器 B 无法自己完成这一步，**必须先在机器 A 上做完**，否则上面的自查永远过不去：

1. 建只读账号，并把 B 的公钥写进它的 `authorized_keys`。建议用 `restrict` +
   `rrsync`（`/usr/share/rsync/scripts/rrsync`，Debian/Ubuntu 随 rsync 附带）把它
   钉死在只读拉取一个目录上：
   `restrict,command="rrsync -ro /var/lib/plum/analytics" ssh-ed25519 AAAA...`。
   手写 `command="rsync --server --sender ..."` 很容易和客户端实际参数对不上，
   表现为 rsync 退出码非 0 而看不出原因。
2. 确认落盘目录 `/var/lib/plum/analytics` 存在，且该账号可读。
   这个路径与机器 A 的 `PLUM_ANALYTICS_SINK_DIR` 必须**逐字符一致**：
   两边漂移的表现是「每次拉取 0 个文件」，和「还没有流量」长得一模一样，
   不报任何错。

## 5. 配置环境

```bash
sudo install -d -o plum_da -g plum_da /srv/plum_landing
sudo tee /etc/plum_da.env >/dev/null <<'ENV'
PLUM_DA_DATABASE_URL=postgresql://plum_da:<密码>@127.0.0.1:<第0步的端口>/plum_da
PLUM_DA_REMOTE=<user>@<机器A>:/var/lib/plum/analytics
PLUM_DA_LANDING=/srv/plum_landing
PLUM_DA_SSH_KEY=/home/plum_da/.ssh/plum_da_pull
ENV
sudo chmod 600 /etc/plum_da.env
sudo chown root:root /etc/plum_da.env
```

`EnvironmentFile` 由 systemd 以 root 读取后注入，所以这个文件不需要给 `plum_da`
读权限；`600 root:root` 是更紧的那一档。

## 6. 首次跑通

以 `plum_da` 身份在 `/opt/plum_da` 里跑，venv 的属主要与服务一致：

```bash
sudo -u plum_da bash -lc '
  cd /opt/plum_da
  python3 -m venv .venv && .venv/bin/pip install -e .
'
sudo -u plum_da bash -lc '
  set -a; . /etc/plum_da.env; set +a
  cd /opt/plum_da
  .venv/bin/python run_ingest.py --dry-run     # 只看能发现哪些文件
'
```

`--dry-run` 报 0 个文件时先别往下走：**这时候分不清「A 侧还没有流量」和
「路径写错了」**。回到第 4 步的自查，直接 `ls` 一眼机器 A 的目录里有没有东西。

确认有文件后再真跑：

```bash
sudo -u plum_da bash -lc '
  set -a; . /etc/plum_da.env; set +a
  cd /opt/plum_da
  .venv/bin/python run_ingest.py                # 迁移 + 建分区 + 拉取 + 装载
'
```

自查：

```sql
SELECT count(*) FROM analytics.product_events;
SELECT file_key, line_count, rows_loaded FROM analytics.ingest_ledger ORDER BY loaded_at DESC LIMIT 5;
```

`rows_loaded` 应等于 `line_count`。不等说明有行解析失败，看运行日志里的 warning。

## 7. 上定时任务

```bash
sudo cp /opt/plum_da/deploy/plum-da-ingest.service \
        /opt/plum_da/deploy/plum-da-ingest.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now plum-da-ingest.timer
systemctl list-timers plum-da-ingest.timer
sudo systemctl start plum-da-ingest.service && systemctl status plum-da-ingest.service
```

最后一条是手动触发一次，验证**以 systemd 的身份和环境**也能跑通——
第 6 步的手工执行只证明了命令本身对，证明不了单元文件对。

失败会以非 0 退出，由 systemd 记录；**装载失败不会自动重试**，
下一次定时执行时那个文件仍在待装列表里，会自然重来。

## 8. 上入库滞后看门

**这一步不是可选的。** `plum-da-ingest.service` 是 `oneshot`，失败只以非 0 退出、由
systemd 记在 journal 里，**没有任何人会收到通知**；而机器 A 只保留 30 天。两者相乘，
B 停拉超过 30 天，那几天的数据就永久没了，且现象和「那几天没流量」完全一样。

```bash
sudo cp /opt/plum_da/deploy/plum-da-watchdog.service \
        /opt/plum_da/deploy/plum-da-watchdog.timer \
        /opt/plum_da/deploy/plum-da-watchdog-ping.service \
        /opt/plum_da/deploy/plum-da-watchdog-ping.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now plum-da-watchdog.timer plum-da-watchdog-ping.timer
```

告警地址追加到 `/etc/plum_da.env`（飞书群机器人 webhook，**从密码管理器取，不要写进仓库**）：

```bash
sudo tee -a /etc/plum_da.env >/dev/null <<'ENV'
PLUM_DA_ALERT_WEBHOOK=<飞书群机器人 webhook>
ENV
```

没配 `PLUM_DA_ALERT_WEBHOOK` 时看门不会静默通过：有异常就以非 0 退出，systemd 至少
记一笔 failed。但「装了看门却没人收到消息」和「没装看门」在效果上一样，别停在这一步。

自查（`--dry-run` 只读，不发送也不写状态）：

```bash
sudo -u plum_da bash -lc '
  set -a; . /etc/plum_da.env; set +a
  cd /opt/plum_da && .venv/bin/python run_watchdog.py --dry-run
'
```

刚装完还没跑过入库时，它应当报 `heartbeat_missing`——**报出来才说明看门是活的**。
跑一次 `plum-da-ingest.service` 之后再看，两条水位都应有值。

### 两个信号分别意味着什么

看门查两件事，含义不同，阈值也不同，**不要把它们当成一回事**：

| 信号 | 变旧说明什么 | 默认阈值 |
| --- | --- | --- |
| 心跳文件 | 入库**进程**没跑完：timer 没触发、PG 连不上、rsync 凭据失效、盘满 | 6 小时 |
| `max(loaded_at)` | 没有**新数据**进库：可能 B 坏了，**也可能只是 A 那段时间没流量** | 26 小时 |

台账水位天然分不清后两种情况，所以窗口必须跨过一整个日夜低谷。海外线深夜本来就可能
几小时没有事件，按 6 小时报会在每个凌晨误报一次——**误报几次之后没人再看这个频道，
连心跳那条真信号也一起废掉了**。阈值可用 `PLUM_DA_WATCHDOG_HEARTBEAT_HOURS` /
`PLUM_DA_WATCHDOG_LEDGER_HOURS` 覆盖，同类告警的再发间隔用
`PLUM_DA_WATCHDOG_COOLDOWN_HOURS`（默认 6 小时，防止停三天刷 72 条）。

心跳只在**全流程跑完且拉取、装载都成功**时更新。`--only` / `--skip-pull` 的手工调试
不刷新它，否则一次调试会掩盖掉之后 6 小时里 timer 其实已经挂了。

### 它覆盖不了什么

⚠️ **机器 B 整机宕机、或 watchdog 自己的 timer 没起来时，本机的一切检查都不会执行**，
表现同样是「一片安静」。本机看门无法自证存活，这需要一个机器 B 之外的 dead-man switch，
当前阶段没有。替代手段是 `plum-da-watchdog-ping.timer`：每天 10:05 主动播报一次
「入库正常」，**由人注意到它的缺席**。这条日报的价值不在内容，在于它没来的那天。

## 9. 装完之后

首次安装到此结束。上线后的日常核对与保留期到期前的确认写在上游仓库的运行手册里，
不在本文重复：`ai4all_bridge` 的 `docs/ops/products/plum/analytics_pipeline.md`。
