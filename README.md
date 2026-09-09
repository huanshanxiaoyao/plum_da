# plum_da — Plum 数仓

机器 B（Lightsail）上的分析侧代码。与业务仓 `ai4all_bridge` 之间**只有文件契约**，
没有网络调用、没有共享数据库、没有共享代码。

## 为什么是独立仓库

- 业务仓的迁移链是**全局单链**：一条只为数仓写的迁移会在只跑朝夕/鸣蝉的国内库上执行。
  数仓表放进去等于让无关环境承担 DDL 风险。
- 数仓的发布节奏与业务发布无关，绑在一起只会互相卡。
- 分析代码读到的是**已落盘的历史数据**，它不该有任何能影响线上请求的路径。

## 数据怎么进来

```
机器 A (aws-sg, ai4all_bridge)          机器 B (Lightsail, 本仓)
  FileSink 写 NDJSON.gz          rsync    run_ingest.py
  切片封口后写 .manifest   ──────────►   校验 manifest → 装 ODS → 台账
```

**manifest 在场 ≡ 文件已封口、可以拉**。这是整条链路唯一的完整性约定：
机器 A 只在切片关闭时用 `os.replace` 原子写出 manifest，正在写的切片没有 manifest。
拉取端不做完整性判断，装载端只认 manifest——判断集中在一处，两边不会各持一套标准。

四条通道见设计文档 §3.0。本仓当前只实现 **C1（行为事件流，尽力而为）**；
C2（删除/身份合并指令，可靠投递）是 W3.5，删除功能开放前必须完成。

## 目录

| 路径 | 职责 |
|---|---|
| `contracts/` | 钉版本的事件字典**只读副本** + 漂移检查（只告警不阻断） |
| `ingest/filespec.py` | 文件契约解析：切片命名、manifest、sha256 |
| `ingest/pull.py` | rsync 拉取（B 主动拉 A，单向） |
| `ingest/rows.py` | 信封 → ODS 行的纯映射 |
| `ingest/load_ods.py` | 校验 + 装载，文件粒度幂等 |
| `ingest/ledger.py` | 文件台账：这个文件装过没有 |
| `migrations/` | 本仓**自己的**迁移链，与业务仓全局单链无关 |
| `warehouse/partitions.py` | ODS 按天分区：提前建、过期删 |
| `warehouse/project.py` | 内测启用日起的访客历史、曝光/点击去重事实 |
| `warehouse/refresh.sql` | 日访客、曝光、被点击曝光与未归因点击汇总 |
| `warehouse/report.py` | 仅读聚合视图，原子生成独立 HTML |
| `run_report.py` | 每日模型刷新与静态只读看板入口 |
| `run_ingest.py` | 入口：迁移 → 建分区 → 拉取 → 装载 → 清理 |

内测看板的需求、技术与验证记录见 [Analytics V1](docs/analytics-v1-requirements.md)，
新增部署步骤见 [部署与回滚](deploy/analytics-v1.md)。不补历史用户或数据。
离线演示不连接数据库：

```bash
.venv/bin/python run_report.py --demo --output /tmp/plum-analytics-preview/index.html
```

## 表设计的两条硬约束

**1. ODS 与信封一一对应，不提列。**
信封实际只有 15 个字段，其余全在 `props` 里。设计文档早期的 DDL 提列了
`request_id` / `character_id` / `surface` / `platform` / `ip_country` 等——
它们**不是信封字段，是各事件的 props**。ODS 是原始落地层，多一列就是多一处解释；
维度提列放到 DWD（W4），那里改一次只要重算，而 ODS 装了数据之后改表就是重建。
高频过滤维度用 JSONB 表达式索引拿回性能。

**2. 分区键是 `server_time`（接收日），不是 `business_day`（业务日）。**
分区要单调才能安全 DROP。business_day 由客户端时间推导，迟到事件会往回落，
用它当分区键意味着「昨天的分区」永远可能再长出新行。代价是整日重算扫不到分区裁剪，
用 `(business_day, event_name)` 索引补回来。

## 本地开发

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest tests/ -q                 # 纯逻辑测试，不需要 PG
PLUM_DA_TEST_DSN=postgresql://... .venv/bin/pytest tests/ -q   # 带集成测试
```

字典漂移检查（需要本地有 `ai4all_bridge` 检出）：

```bash
.venv/bin/python contracts/drift_check.py --upstream ../ai4all_bridge/tracking/plum_events.yaml
```

**只告警，永不阻断**：数仓的构建门禁挡不住上游合并，只会让上游改字典后数仓无法发布，
既不能阻止漂移，又把两个仓库的发布节奏绑死。漂移的正确响应是人看一眼、
决定是否跟进并重新钉版本（改 `contracts/PINNED`）。

## 部署

见 [`deploy/README.md`](deploy/README.md)。
