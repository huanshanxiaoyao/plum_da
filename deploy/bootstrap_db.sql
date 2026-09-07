-- 机器 B 分析库初始化。以超级用户执行一次。
--
-- ⚠️ 密码不要写进这个文件。执行时用 psql 变量传入：
--     psql -v role_password="$(read -s ...)" -f deploy/bootstrap_db.sql
--
-- ⚠️ PG15+ 取消了 PUBLIC 对 public schema 的隐式 CREATE 权限。
--    下面的 GRANT 不是可选项：缺了它，迁移在第一条 CREATE SCHEMA 就失败。
--
-- 本脚本可重复执行：部署流程要求「自查不通过就停下」，停下之后必然要重来，
-- 一个第二次执行就报 already exists 的脚本会把人逼去手工拆解。
-- 重跑时口令按传入值**收敛**（ALTER ROLE），不是保留旧值——否则密码改过之后
-- 重跑会静默地留下与 /etc/plum_da.env 不一致的口令，表现为连不上而无从查起。

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plum_da') THEN
        CREATE ROLE plum_da LOGIN;
    END IF;
END
$$;

ALTER ROLE plum_da LOGIN PASSWORD :'role_password';

-- CREATE DATABASE 不能出现在 DO 块（事务）里，用 \gexec 做条件执行。
SELECT 'CREATE DATABASE plum_da OWNER plum_da'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'plum_da')
\gexec

\connect plum_da

GRANT USAGE, CREATE ON SCHEMA public TO plum_da;

-- 自查：下面两行都应返回 t，否则不要继续。
SELECT has_schema_privilege('plum_da', 'public', 'CREATE') AS can_create_in_public;
SELECT has_database_privilege('plum_da', 'plum_da', 'CONNECT') AS can_connect;
