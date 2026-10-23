#!/bin/bash
# 在真实 MySQL 上验证迁移与租户隔离。
#
# 一次跑完所有步骤再退出——后台常驻进程会随 shell 结束被收走，
# 跨调用时数据目录还会被锁。这个脚本把「启动→验证→关闭」收在一起。
#
# 用法：./scripts/verify_mysql.sh

set -uo pipefail

MYSQL_HOME=${MYSQL_HOME:-/tmp/fcircle-mysql/mysql-8.0.40-macos14-arm64}
DATA_DIR=${DATA_DIR:-/tmp/fcircle-mysql/data}
PORT=${PORT:-3399}
BIN="$MYSQL_HOME/bin"
PROJECT=$(cd "$(dirname "$0")/.." && pwd)

cd "$PROJECT" || exit 1

cleanup() {
    "$BIN/mysqladmin" -h127.0.0.1 -P"$PORT" -uroot shutdown 2>/dev/null
    sleep 3
    pkill -f "fcircle-mysql.*mysqld" 2>/dev/null
    sleep 1
    pkill -9 -f "fcircle-mysql.*mysqld" 2>/dev/null
    return 0
}
trap cleanup EXIT

echo "=============================================================="
echo "真实 MySQL 验证"
echo "=============================================================="
echo "版本目录: $MYSQL_HOME"

# ---- 1. 清理并启动 ----
pkill -9 -f "fcircle-mysql.*mysqld" 2>/dev/null
sleep 2

"$BIN/mysqld" --basedir="$MYSQL_HOME" --datadir="$DATA_DIR" \
    --socket=/tmp/fcircle-verify.sock --port="$PORT" \
    --bind-address=127.0.0.1 \
    --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci \
    --skip-log-bin --max-connections=100 \
    > /tmp/fcircle-verify.log 2>&1 &

MYSQL_PID=$!

echo -n "等待 MySQL 就绪"
for i in $(seq 1 40); do
    if "$BIN/mysql" -h127.0.0.1 -P"$PORT" -uroot -e "SELECT 1" >/dev/null 2>&1; then
        echo " [就绪]"
        break
    fi
    echo -n "."
    sleep 1
    if [ "$i" -eq 40 ]; then
        echo " [超时]"
        tail -20 /tmp/fcircle-verify.log
        exit 1
    fi
done

export MYSQL_HOST=127.0.0.1
export MYSQL_PORT="$PORT"
export MYSQL_USER=root
export MYSQL_PASSWORD=""
export MYSQL_DATABASE=fcircle_verify
export SECRET_KEY=verify-only-secret-key-not-for-production

"$BIN/mysql" -h127.0.0.1 -P"$PORT" -uroot << 'SQL'
DROP DATABASE IF EXISTS fcircle_verify;
CREATE DATABASE fcircle_verify CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
SQL
echo "数据库已创建"

# ---- 2. 跑迁移 ----
echo ""
echo "--- [2] alembic upgrade head ---"
if ! .venv/bin/alembic upgrade head 2>&1 | tail -5; then
    echo "[FAIL] 迁移失败"
    exit 1
fi
echo "[OK] 迁移成功"

# ---- 3. 检查表结构 ----
echo ""
echo "--- [3] 表结构检查 ---"
"$BIN/mysql" -h127.0.0.1 -P"$PORT" -uroot fcircle_verify -e "
SELECT TABLE_NAME AS '表', TABLE_ROWS AS '行数估算', ENGINE AS '引擎',
       TABLE_COLLATION AS '排序规则'
FROM information_schema.TABLES
WHERE TABLE_SCHEMA='fcircle_verify' ORDER BY TABLE_NAME;"

echo ""
echo "--- 索引与唯一约束 ---"
"$BIN/mysql" -h127.0.0.1 -P"$PORT" -uroot fcircle_verify -e "
SELECT TABLE_NAME AS '表', INDEX_NAME AS '索引',
       GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS '列',
       IF(NON_UNIQUE=0,'唯一','普通') AS '类型'
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA='fcircle_verify' AND TABLE_NAME NOT LIKE 'alembic%'
GROUP BY TABLE_NAME, INDEX_NAME, NON_UNIQUE
ORDER BY TABLE_NAME, INDEX_NAME;" 2>&1 | head -40

# ---- 4. 运行真实 ORM 集成测试 ----
echo ""
echo "--- [4] 真实 MySQL 上的租户隔离验证 ---"
.venv/bin/python scripts/mysql_integration_check.py

echo ""
echo "=============================================================="
echo "验证完成"
echo "=============================================================="