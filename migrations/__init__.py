"""分析库自己的迁移链，与 `ai4all_bridge` 的全局单链**完全无关**（设计文档 §8.1 论据 4）。

主仓库的迁移链是全局单链：一条只为 Plum 写的迁移也会在只跑朝夕/鸣蝉的国内库上执行。
数仓的表放进去等于让国内库承担与它无关的 DDL 风险，所以这里另起一条链。

链是「有序、只追加」的：改已发布的迁移等于让不同环境得到不同 schema。要改就追加一条。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Tuple

logger = logging.getLogger("plum_da.migrations")

SCHEMA = "analytics"

# ---------------------------------------------------------------------------
# ODS 表结构与机器 A 的信封**一一对应**。
#
# 设计文档 §8.2 早期的 DDL 提列了 request_id / character_id / surface / position /
# rank_version / platform / ip_country / exp_variants 等，但它们在实际信封里**不存在**
# ——都是各事件的 props 字段。ODS 是原始落地层，多一列就是多一处解释；提列放到 DWD
# （W4）去做，那里改一次只要重算，而 ODS 装了数据之后改表就是重建。
#
# 高频过滤维度用 JSONB 表达式索引拿回性能，不用假装它们是列。
# ---------------------------------------------------------------------------

_M001 = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

CREATE TABLE IF NOT EXISTS {SCHEMA}.product_events (
  -- 信封字段（顺序与 ai4all_bridge 的 emit 构造保持一致，便于逐字段核对）
  event_id         UUID        NOT NULL,
  event_name       TEXT        NOT NULL,
  dict_version     TEXT        NOT NULL,
  client_time      BIGINT,                       -- epoch 毫秒，原样保留不转型
  skew0_ms         INTEGER,                     -- 005 起放宽为 BIGINT，见下
  session_id       TEXT,
  props            JSONB       NOT NULL DEFAULT '{{}}'::jsonb,
  server_time      TIMESTAMPTZ NOT NULL,         -- 分区键：接收日
  clock_skew_ms    INTEGER,                     -- 005 起放宽为 BIGINT，见下
  event_time_utc   TIMESTAMPTZ,
  business_day     DATE        NOT NULL,         -- 事实层唯一日切（D17），非分区键
  time_fallback    TEXT,                         -- future | too_late | no_skew0
  subject_kind     TEXT,
  visitor_id       TEXT,
  platform_user_id TEXT,
  -- 装载元数据：让「这条从哪个文件来」可回答，也是按文件重放的依据
  source_file      TEXT        NOT NULL,
  loaded_at        TIMESTAMPTZ NOT NULL DEFAULT now()
) PARTITION BY RANGE (server_time);
"""

_M002 = f"""
-- 分区表上不能建全局唯一约束（必须含分区键），所以这里只有非唯一索引。
-- 去重不靠约束，靠装载层的「按文件原子替换」+ 台账（§8.3）。
CREATE INDEX IF NOT EXISTS product_events_server_time_event_name_idx
  ON {SCHEMA}.product_events (server_time, event_name);
-- ★ 整日重算靠它：分区键是接收日，重算的过滤条件却是业务日，二者不重合（§8.3）。
CREATE INDEX IF NOT EXISTS product_events_business_day_event_name_idx
  ON {SCHEMA}.product_events (business_day, event_name);
CREATE INDEX IF NOT EXISTS product_events_platform_user_idx
  ON {SCHEMA}.product_events (platform_user_id, server_time)
  WHERE platform_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS product_events_visitor_idx
  ON {SCHEMA}.product_events (visitor_id, server_time)
  WHERE visitor_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS product_events_source_file_idx
  ON {SCHEMA}.product_events (source_file);
-- request_id 在 props 里而不是列上（见上方说明），用表达式索引拿回 CTR 归因的过滤性能。
CREATE INDEX IF NOT EXISTS product_events_request_id_idx
  ON {SCHEMA}.product_events ((props ->> 'request_id'))
  WHERE props ? 'request_id';
"""

# 死信不分区：量级是主通道的千分之一，按天分区的维护成本远大于收益，
# 保留期用 DELETE 就够。字段同样按机器 A 的真实死信记录逐字段抄录——
# **绝不整包透传 payload**：死信恰恰是唯一没经过脱敏的路径（§8.2 警告）。
_M003 = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.product_events_dead (
  id             BIGSERIAL PRIMARY KEY,
  reason         TEXT        NOT NULL,
  event_id       UUID,                          -- 005 起放宽为 TEXT，见下
  event_name     TEXT,
  dict_version   TEXT,
  session_id     TEXT,
  client_time    BIGINT,
  server_time    TIMESTAMPTZ NOT NULL,
  business_day   DATE        NOT NULL,           -- 死信按接收日分区（时间字段本就不可信）
  payload_sha256 TEXT,                           -- 只有哈希，没有原包
  detail         TEXT[],                         -- 只有「字段路径:错误类型」，没有值
  source_file    TEXT        NOT NULL,
  loaded_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS product_events_dead_day_reason_idx
  ON {SCHEMA}.product_events_dead (business_day, reason);
CREATE INDEX IF NOT EXISTS product_events_dead_source_file_idx
  ON {SCHEMA}.product_events_dead (source_file);
"""

# 文件台账（§8.3 L1）。它回答两个问题：这个文件拉过没有、装过没有。
# sha256 存下来是为了让「同名不同内容」能被发现——那意味着上游写坏了或有人手改过文件。
_M004 = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.ingest_ledger (
  file_key     TEXT        PRIMARY KEY,          -- lane/business_day/文件名
  lane         TEXT        NOT NULL,             -- data | dead
  business_day DATE        NOT NULL,
  writer_id    TEXT        NOT NULL,
  slice_index  INTEGER     NOT NULL,
  sha256       TEXT        NOT NULL,
  line_count   INTEGER     NOT NULL,
  first_ts     TEXT,
  last_ts      TEXT,
  closed_at    TEXT,
  loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  rows_loaded  INTEGER     NOT NULL
);
CREATE INDEX IF NOT EXISTS ingest_ledger_day_idx
  ON {SCHEMA}.ingest_ledger (business_day, lane);
"""

# 两处「字段类型比上游取值域窄」的修正。合并成一条迁移，因为它们是同一类错误：
# **把字段的「正常取值」当成「可能取值」来定类型**，而这两列都在装载的必经之路上,
# 类型放不下就是整份文件 COPY 失败。
#
# ① `skew0_ms` / `clock_skew_ms`：INTEGER 只到 ±2147483647 毫秒（约 24.9 天）。
#    这两列都是**客户端时钟偏差**、不是时长——上游 `EventBatchWire.skew0_ms` 与
#    `client_time` 都没有取值上限，一台把日期设成 1970 年的设备就能造出 1.7e12 的偏差。
#    溢出不需要「迟到 25 天」这种边角场景，一个坏客户端就够。为一个**诊断值**放不下
#    而丢掉整份文件的真实事件，代价完全不对等，故放宽到 BIGINT。
# ② `product_events_dead.event_id`：死信里的 event_id **本来就可能不是 UUID**——
#    上游 `_dead_letter` 只判 `isinstance(raw_id, str)` 就原样落盘，而 `bad_envelope`
#    这一类死信的成因往往正是「event_id 不合法」。用 UUID 存等于要求死信先合法，
#    自相矛盾；代价是这类死信文件永远装不进去，并连累同批次的正常文件。
_M005 = f"""
ALTER TABLE {SCHEMA}.product_events
  ALTER COLUMN skew0_ms TYPE BIGINT,
  ALTER COLUMN clock_skew_ms TYPE BIGINT;
ALTER TABLE {SCHEMA}.product_events_dead
  ALTER COLUMN event_id TYPE TEXT USING event_id::TEXT;
"""


# 「新访客」的权威口径。**不是** `visitor_first_seen` 的事件计数。
#
# 铸 visitor_id 的动作已经搬到浏览器（`plum_chat/lib/analytics/visitor.ts`），这消灭了
# 服务端时代「首屏几个并发请求各铸一个 id」的竞态。但铸造本身仍可能重复：两个 tab 同时
# 冷启、cookie 被拦截后每次访问重铸、用户清了浏览器数据。拿事件计数当新访客数，会被这些
# 重复**直接**污染；拿 `min(server_time) group by visitor_id` 推导则不会——重复铸出来的
# 是不同的 visitor_id，本来就该算不同的浏览器，而同一个 visitor_id 无论收到几条
# `visitor_first_seen` 都只贡献一个首见时刻。GA4 / Snowplow 都是这么分的两层。
#
# 用视图而不是物化表：ODS 是每天追加的，物化就要跟着调度重算，而重算逻辑写错的代价是
# 一个**看不出错**的错数字。等真的慢到影响使用再物化（二期），那时也只是加一层缓存。
#
# `first_seen` 取 `server_time` 而不是 `event_time_utc`：后者依赖客户端时钟与 skew 修正，
# 一台时钟设错的设备能把自己的首见推到 1970 年，整个「新访客趋势」被一条记录拖歪。
# 接收时刻是服务端自己盖的章，无法被客户端影响。
_M006 = f"""
CREATE OR REPLACE VIEW {SCHEMA}.dim_visitor AS
SELECT
  visitor_id,
  min(server_time)                        AS first_seen,
  max(server_time)                        AS last_seen,
  min(business_day)                       AS first_business_day,
  count(*)                                AS event_count
FROM {SCHEMA}.product_events
WHERE visitor_id IS NOT NULL
GROUP BY visitor_id;

COMMENT ON VIEW {SCHEMA}.dim_visitor IS
  '访客维表：新访客数的权威口径。按 min(server_time) 推导首见，不要用 visitor_first_seen 的事件计数。';
"""


# (id, sql)。id 一旦发布不得改动，只能追加。
MIGRATIONS: List[Tuple[str, str]] = [
    ("001_ods_product_events", _M001),
    ("002_ods_indexes", _M002),
    ("003_ods_dead", _M003),
    ("004_ingest_ledger", _M004),
    ("005_widen_skew_and_dead_event_id", _M005),
    ("006_dim_visitor", _M006),
    ("007_persistent_analytics", Path(__file__).with_name("007_analytics.sql").read_text()),
]

_VERSION_TABLE = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE TABLE IF NOT EXISTS {SCHEMA}.schema_migrations (
  id         TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def apply_migrations(conn: Any) -> List[str]:
    """把未应用的迁移按序执行，返回本次实际应用的 id 列表。幂等。

    每条迁移单独提交：一条失败不该让已成功的几条回滚，否则重跑时状态更难判断。
    """

    applied: List[str] = []
    with conn.cursor() as cur:
        cur.execute(_VERSION_TABLE)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f"SELECT id FROM {SCHEMA}.schema_migrations")
        done = {row[0] for row in cur.fetchall()}

    for migration_id, sql in MIGRATIONS:
        if migration_id in done:
            continue
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(f"INSERT INTO {SCHEMA}.schema_migrations (id) VALUES (%s)", (migration_id,))
        conn.commit()
        applied.append(migration_id)
        logger.info("applied migration %s", migration_id)
    return applied
