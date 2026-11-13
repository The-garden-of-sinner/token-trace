#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 README 演示截图用的**虚构**会话数据。

    python3 -B scripts/make_demo.py [输出目录]        # 默认 ./.demo-data
    WORKBUDDY_HOME=<输出目录> python3 -B trace_server.py --port 8917

场景是「订单服务 /checkout 接口 P99 延迟排查」。所有路径、命令、日志、代码、
提问、回答、trace_id、token 数都是示例，不读取任何真实会话或 workbuddy.db；
模型 ID 为平台公开标识。数值按 README 里的峰档单价公式生成，积分 / 命中率 /
未命中三者自洽，便于截图看起来像一次真实排查而不是占位符。
"""
import json
import random
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ".demo-data").resolve()
# 截图要看起来像「刚刚发生」：锚点取当前时间往回 6 天。内容仍由固定种子生成，
# 因此每次跑出来的文案、数字一致，只有日期跟着当天走。
BASE = (datetime.now(timezone.utc) - timedelta(days=6)).replace(
    hour=1, minute=12, second=4, microsecond=0)
T0 = int(BASE.timestamp() * 1000)

MODEL = "deepseek-v4.1-flash"          # 平台公开的模型 ID
MODEL_NAME = "DeepSeek V4.1 Flash"
CWD = "/home/linwei/work/order-service"
PROJ_DIR = "home-linwei-work-order-service"
R = random.Random(20260302)


def hid(n=16):
    return "".join(R.choice("0123456789abcdef") for _ in range(n))


def sid_of(name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "token-trace/demo/" + name))


# ---------------------------------------------------------------- 输出素材
# 全部由固定种子生成，长度和排版刻意接近真实终端输出。

def slow_logs(n=54):
    t = datetime(2026, 3, 2, 8, 58, 11, tzinfo=timezone.utc)
    out = ["$ kubectl -n prod logs deploy/order-api --since=2h | grep 'slow request' | tail -%d" % n]
    for _ in range(n):
        t += timedelta(seconds=R.randint(2, 14), milliseconds=R.randint(0, 999))
        dur = R.randint(830, 2380)
        out.append(
            "%s.%03dZ  WARN  order-api-7d9c4b8f-%s  trace_id=%s span_id=%s "
            "route=/v1/checkout status=200 dur=%dms db_wait=%dms pool_wait=%dms "
            "user_id=88%d order_no=SO20260302%d msg=\"slow request exceeded p99 budget\""
            % (t.strftime("%Y-%m-%dT%H:%M:%S"), t.microsecond // 1000, hid(5), hid(),
               hid(8), dur, int(dur * R.uniform(.78, .88)), int(dur * R.uniform(.55, .68)),
               R.randint(10000, 99999), R.randint(1000, 9999)))
    out.append("-- %d 条，其中 %d 条 pool_wait > 500ms" % (n, int(n * .81)))
    return "\n".join(out)


def prom_metrics():
    rows = [
        ("order_checkout_duration_seconds_bucket{le=\"0.2\"}", R.randint(38100, 39900)),
        ("order_checkout_duration_seconds_bucket{le=\"0.5\"}", R.randint(41200, 43800)),
        ("order_checkout_duration_seconds_bucket{le=\"1\"}", R.randint(45100, 46900)),
        ("order_checkout_duration_seconds_bucket{le=\"2.5\"}", R.randint(47800, 48900)),
        ("order_checkout_duration_seconds_bucket{le=\"+Inf\"}", R.randint(49000, 49900)),
    ]
    out = ["# HELP order_checkout_duration_seconds End-to-end latency of /v1/checkout",
           "# TYPE order_checkout_duration_seconds histogram"]
    out += ["%s %d" % (k, v) for k, v in rows]
    out += ["# TYPE db_pool_wait_seconds summary",
            "db_pool_wait_seconds_sum 4183.72", "db_pool_wait_seconds_count 49211",
            "# TYPE db_pool_in_use gauge", "db_pool_in_use{pool=\"order-primary\"} 40",
            "db_pool_max_open{pool=\"order-primary\"} 40",
            "# TYPE go_goroutines gauge", "go_goroutines 1843",
            "# TYPE go_gc_duration_seconds summary", "go_gc_duration_seconds{quantile=\"0.99\"} 0.0318"]
    for m in ["query_orders_by_user", "insert_order", "update_inventory", "charge_payment"]:
        out.append("order_db_query_seconds_sum{op=\"%s\"} %.2f" % (m, R.uniform(20, 190)))
        out.append("order_db_query_seconds_count{op=\"%s\"} %d" % (m, R.randint(8000, 26000)))
    out.append("order_cache_hit_ratio{cache=\"sku\"} 0.72")
    out.append("order_cache_hit_ratio{cache=\"coupon\"} 0.41")
    return "\n".join(out)


def handler_go():
    return '''internal/checkout/handler.go
  1  package checkout
  2
  3  import (
  4      "context"
  5      "net/http"
  6      "time"
  7
  8      "order-service/internal/db"
  9      "order-service/internal/cache"
 10  )
 11
 12  type Handler struct {
 13      store  *db.Store
 14      sku    *cache.LRU
 15      client *http.Client
 16  }
 17
 18  func (h *Handler) Checkout(w http.ResponseWriter, r *http.Request) {
 19      ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
 20      defer cancel()
 21
 22      uid := r.Header.Get("X-User-Id")
 23      orders, err := h.store.RecentOrders(ctx, uid, 20)
 24      if err != nil {
 25          http.Error(w, "query failed", http.StatusInternalServerError)
 26          return
 27      }
 28      // 每次请求都会回查一遍历史订单用于风控，这里没有走索引
 29      for _, o := range orders {
 30          _ = h.sku.Get(ctx, o.SKUID)
 31      }
 32      writeJSON(w, map[string]any{"orders": len(orders)})
 33  }
 34
 35  func (s *Store) RecentOrders(ctx context.Context, uid string, limit int) ([]Order, error) {
 36      const q = `SELECT id, user_id, sku_id, amount, status, created_at
 37                 FROM orders WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2`
 38      rows, err := s.pool.Query(ctx, q, uid, limit)
 39      if err != nil {
 40          return nil, fmt.Errorf("recent orders: %w", err)
 41      }
 42      defer rows.Close()
 43      // ...
 44  }'''


def pg_stat():
    out = ["  count | state  |   wait_event_type   |      query"]
    out.append("--------+--------+---------------------+------------------")
    for c, st, we, q in [(38, "active", "Client", "SELECT ... FROM orders WHERE user_id"),
                         (2, "idle in transaction", "Client", "COMMIT"),
                         (1, "active", "IO", "SELECT ... FROM inventory"),
                         (1, "idle", "Timeout", "pg_sleep")]:
        out.append("  %-5d | %-20s | %-19s | %s" % (c, st, we, q))
    out.append("(42 rows)")
    out.append("")
    out.append(" max_connections | used | reserved_for_super")
    out.append("-----------------+------+-------------------")
    out.append("             100 |   42 |                 3")
    return "\n".join(out)


def explain():
    return '''                                                                  QUERY PLAN
---------------------------------------------------------------------------------------------------------------
 Limit  (cost=0.00..18244.31 rows=20 width=96) (actual time=713.482..713.501 rows=20 loops=1)
   ->  Seq Scan on orders  (cost=0.00..18244.31 rows=318 width=96)
         (actual time=0.041..702.883 rows=24817 loops=1)
         Filter: (user_id = '88910427'::text)
         Rows Removed by Filter: 4812933
         Buffers: shared hit=1823 read=91244 dirtied=17
 Planning Time: 0.184 ms
 Execution Time: 713.548 ms
(8 rows)

$ psql -h pg-order.internal -d orderdb -c "\\d orders" | grep -E 'Index|user_id'
Indexes:
    "orders_pkey" PRIMARY KEY, btree (id)
    "orders_created_at_idx" btree (created_at DESC)
    "orders_status_idx" btree (status)
-- 注意：user_id 上没有索引，且 created_at 索引无法用于等值过滤后的排序下推

$ psql -h pg-order.internal -d orderdb -c "SELECT n_live_tup, last_analyze FROM pg_stat_user_tables WHERE relname='orders'"
 n_live_tup |         last_analyze
------------+-------------------------------
    4837750 | 2026-02-27 03:12:44.118+00
(1 row)'''


def git_diff():
    return '''diff --git a/internal/checkout/handler.go b/internal/checkout/handler.go
index 3a1c9f2..8e7b441 100644
--- a/internal/checkout/handler.go
+++ b/internal/checkout/handler.go
@@ -20,12 +20,14 @@ func (h *Handler) Checkout(w http.ResponseWriter, r *http.Request) {
        ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
        defer cancel()

-       uid := r.Header.Get("X-User-Id")
-       orders, err := h.store.RecentOrders(ctx, uid, 20)
+       uid := r.Header.Get("X-User-Id")
+       key := r.Header.Get("X-Idempotency-Key")
+       if key == "" {
+           http.Error(w, "missing idempotency key", http.StatusBadRequest)
+           return
+       }
+       orders, err := h.store.RecentOrders(ctx, uid, 20)
        if err != nil {
            http.Error(w, "query failed", http.StatusInternalServerError)
            return
        }
-       // 每次请求都会回查一遍历史订单用于风控，这里没有走索引
-       for _, o := range orders {
-           _ = h.sku.Get(ctx, o.SKUID)
-       }
+       skus, err := h.sku.BatchGet(ctx, orderSKUs(orders))
+       if err != nil {
+           log.Warn("sku batch degraded", "err", err)
+       }
        writeJSON(w, map[string]any{"orders": len(orders), "skus": len(skus)})
    }
diff --git a/migrations/20260302_add_orders_user_id_idx.sql b/migrations/20260302_add_orders_user_id_idx.sql
new file mode 100644
--- /dev/null
+++ b/migrations/20260302_add_orders_user_id_idx.sql
@@ -0,0 +1,3 @@
+-- 线上建索引必须 CONCURRENTLY，避免长事务阻塞写入
+CREATE INDEX CONCURRENTLY IF NOT EXISTS orders_user_id_created_at_idx
+    ON orders (user_id, created_at DESC);'''


def k6_out():
    return '''          /\\      |‾‾| /‾‾/   /‾‾/
     /\\  /  \\     |  |/  /   /  /
    /  \\/    \\    |     (   /   ‾‾\\
   /          \\   |  |\\  \\ |  (‾)  |
  / __________ \\  |__| \\__\\ \\_____/ .io

  execution: local
     script: scripts/checkout.js
     output: -

  scenarios: (100.00%) 1 scenario, 80 max VUs, 3m30s max duration
           * checkout: 80 looping VUs for 3m0s

  ✓ status is 200
  ✓ latency < 800ms

  checks.........................: 99.61%  ✓ 142831       ✗ 559
  data_received..................: 187 MB  1.0 MB/s
  data_sent......................: 42 MB   233 kB/s
  http_req_duration..............: avg=186.41ms min=21.03ms med=142.77ms max=2.31s p(90)=318.22ms p(95)=441.09ms
    { expected_response:true }...: avg=185.92ms min=21.03ms med=142.61ms max=2.31s p(95)=440.18ms
  http_req_failed................: 0.00%   ✓ 0           ✗ 143390
  http_reqs......................: 143390  796.611545/s
  iteration_duration.............: avg=199.78ms min=24.11ms med=155.90ms max=2.34s p(95)=462.31ms
  iterations.....................: 143390  796.611545/s
  vus............................: 80      min=80        max=80
  vus_max........................: 80      min=80        max=80

running (3m00.0s), 00/80 VUs, 143390 complete and 0 interrupted iterations
checkout ✓ [======================================] 80 VUs  3m0s'''


def pprof_out():
    out = ["(pprof) top20", "Showing nodes accounting for 2.91s, 84.30% of 3.45s total"]
    out.append("      flat  flat%   sum%        cum   cum%")
    for flat, cum, fn in [
        (0.81, 1.24, "database/sql.(*DB).conn"),
        (0.46, 0.46, "runtime.selectgo"),
        (0.38, 0.91, "order-service/internal/db.(*Store).RecentOrders"),
        (0.29, 0.29, "runtime.mallocgc"),
        (0.22, 0.63, "order-service/internal/cache.(*LRU).Get"),
        (0.19, 0.19, "sync.(*Mutex).Lock"),
        (0.17, 0.17, "github.com/jackc/pgx/v5.(*Conn).Query"),
        (0.14, 0.14, "net/http.(*conn).serve"),
        (0.13, 0.13, "encoding/json.Marshal"),
        (0.12, 0.12, "runtime.gcBgMarkWorker")]:
        out.append("     %4.2fs %5.2f%% %5.2f%%      %4.2fs %5.2f%%  %s" %
                   (flat, flat / 3.45 * 100, 0, cum, cum / 3.45 * 100, fn))
    out.append("")
    out.append("goroutine profile: total 1843")
    out.append("1678 @ 0x43ab12 0x42f880 0x7f1c08 0x7f2d41 0x6c91a0")
    out.append("#    0x7f2d41    database/sql.(*DB).conn+0x3a1    /usr/local/go/src/database/sql/sql.go:1298")
    out.append("#    0x6c91a0    internal/db.(*Store).RecentOrders+0x88  internal/db/order.go:41")
    return "\n".join(out)


def gotest():
    return '''ok      order-service/internal/checkout    1.842s  coverage: 87.4% of statements
ok      order-service/internal/db          0.913s  coverage: 79.1% of statements
--- FAIL: TestBatchGetConcurrent (0.12s)
    lru_test.go:118: expected single loader call, got 37
    lru_test.go:119: goroutine leak: 12 blocked on cache.mu
FAIL    order-service/internal/cache       0.431s
FAIL
$ go test ./internal/cache/... -race -count=1 -run TestBatchGetConcurrent -v
=== RUN   TestBatchGetConcurrent
WARNING: DATA RACE
Write at 0x00c00011a018 by goroutine 24:
  order-service/internal/cache.(*LRU).Get()
      internal/cache/lru.go:63 +0x84
Previous read at 0x00c00011a018 by goroutine 23:
  order-service/internal/cache.(*LRU).Get()
      internal/cache/lru.go:58 +0x31
==================
--- PASS: TestBatchGetConcurrent (0.09s)
PASS
ok      order-service/internal/cache       1.207s'''


def ci_yaml():
    return '''name: ci
on:
  pull_request:
    branches: [main]
  push:
    branches: [main]
jobs:
  test:
    runs-on: [self-hosted, linux, kylin-v10]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version: '1.22'
      - name: vet
        run: go vet ./...
      - name: test
        run: go test -race -count=1 ./...
      - name: build
        run: CGO_ENABLED=0 go build -o bin/order-api ./cmd/api
      - name: image
        if: github.ref == 'refs/heads/main'
        run: |
          docker build -t registry.internal/order-api:${{ github.sha }} .
          docker push registry.internal/order-api:${{ github.sha }}
# 注意 vet 步骤在 -race 之前，缓存竞争要等 test 才会暴露'''


def cb_summary():
    return """<cb_summary>
本轮对话围绕 order-service 的 /v1/checkout 接口 P99 延迟从 180ms 升至 1.2s 展开。
已确认的事实：
- 慢请求日志 54 条集中在 09:02~09:11，pool_wait 中位数 710ms，占耗时 60% 以上。
- Prometheus 显示 db_pool_in_use 长期贴着 max_open=40；pg_stat_activity 里 38 个
  active 连接全卡在 RecentOrders 的查询上。
- EXPLAIN ANALYZE 证实 orders 表在 user_id 上走 Seq Scan，扫描 481 万行、耗时 713ms；
  orders 表现存 483 万行，上一次 analyze 是 2026-02-27。
- 迁移 20260302_add_orders_user_id_idx.sql 尚未在 prod 执行（migrate status 显示 pending）。
- 预发 k6 压测 80 VU / 3min：p95 441ms，比目标 200ms 仍偏高，goroutine profile
  里 1678 个协程阻塞在 database/sql.(*DB).conn。
- HEAD~3 的改动同时引入了幂等键校验与 sku.BatchGet，后者未加 singleflight，
  压测毛刺与缓存击穿有关。
待办：给 internal/cache/lru.go 的 BatchGet 补 singleflight 与单测，跑通 CI 后
在 prod 用 CONCURRENTLY 建索引，最后整理根因与上线步骤。
</cb_summary>"""


def generic_out(tool, n):
    """短输出：grep / git status / top 之类，保持行数和排版自然。"""
    if tool == "Grep":
        return "\n".join(
            ["internal/db/pool.go:%d:    pool.SetMaxOpenConns(%d)" % (18 + i * 3, 40 - i)
             for i in range(6)] +
            ["internal/db/pool.go:41:    pool.SetConnMaxLifetime(30 * time.Minute)",
             "internal/cache/lru.go:58:    if v, ok := l.m[k]; ok {",
             "internal/cache/lru.go:63:    l.m[k] = v",
             "-- 9 matches in 3 files"])
    if tool == "Bash":
        return "\n".join(
            ["NAME                              CPU(cores)   MEMORY(bytes)",
             "order-api-7d9c4b8f-%s   182m         312Mi" % hid(5),
             "order-api-7d9c4b8f-%s   176m         298Mi" % hid(5),
             "order-api-7d9c4b8f-%s   191m         327Mi" % hid(5),
             "",
             "$ git status --short",
             " M internal/cache/lru.go",
             " M internal/checkout/handler.go",
             "?? internal/cache/lru_singleflight_test.go",
             "?? migrations/20260302_add_orders_user_id_idx.sql"])
    return "\n".join(["%s: ok" % tool] + ["  line %d: %s" % (i, hid(12)) for i in range(1, n)])


# ---------------------------------------------------------------- 会话内容
def C(tool, args, out, think, say):
    return dict(tool=tool, args=args, out=out, think=think, say=say)


def B(cmd, out, think, say):
    return C("Bash", {"command": cmd}, out, think, say)


def Rf(path, out, think, say):
    return C("Read", {"file_path": path}, out, think, say)


MAIN = dict(
    sid=sid_of("order-service-p99"),
    day_off=0,
    title="订单服务 /checkout P99 延迟排查：慢查询与连接池",
    turns=[
        dict(q="线上 /v1/checkout 的 P99 从 180ms 涨到 1.2s，昨晚 22:10 左右开始，先帮我看下慢在哪",
             inj=("""<system-reminder data-role="user-context">
<project_context>
工作区：/home/linwei/work/order-service
运行时：Go 1.22 / PostgreSQL 15 / Redis 7，prod 与 staging 各 3 副本
分支 main，HEAD a7c31e9
未提交改动：internal/cache/lru.go、internal/checkout/handler.go
监控：/v1/checkout 的 p99 超过 800ms 已持续 12 分钟，告警级别 P2
</project_context>
<conventions>
提交信息遵循 Conventional Commits；线上 DDL 一律用 CONCURRENTLY；
禁止直接改 prod 配置，变更走 migrations + 发布流水线；
排查结论需给出可复现的命令与实测数值。
</conventions>
</system-reminder>"""),
             calls=[
                 B("kubectl -n prod logs deploy/order-api --since=2h | grep 'slow request' | tail -54",
                   slow_logs(),
                   "先把慢请求按时间排一下，看是不是集中在某个时间点，以及 db_wait 和 pool_wait 谁占大头。",
                   "抓到 54 条慢请求，集中在 09:02~09:11。共同点是 pool_wait 中位数 710ms、占整条请求耗时六成以上，DB 执行本身只有 120ms 左右。看起来是连接池排队，不是单条 SQL 突然变慢。"),
                 B("curl -sS http://127.0.0.1:9090/metrics | grep -E 'order_|db_pool|go_goroutines'",
                   prom_metrics(),
                   "指标里如果 in_use 贴着 max_open，就能确认是池子被打满而不是 GC 抖动。",
                   "指标对上了：db_pool_in_use=40、max_open=40，池子长期打满；go_goroutines 1843，比平时高一个量级。另外 sku / coupon 两个缓存命中率只有 0.72 / 0.41，可能还有缓存击穿。先把代码和活跃连接拉出来。"),
                 Rf("internal/checkout/handler.go", handler_go(),
                    "handler 里 RecentOrders 每次都查 20 条历史订单，注释也写了没走索引，这大概就是池子被打满的源头。",
                    "看到了：Checkout 每次请求都调 RecentOrders(uid, 20) 做风控，SQL 按 user_id 过滤再按 created_at 排序。orders 表 483 万行，user_id 上没索引——这条查询基本就是全表扫。"),
             ]),
        dict(q="把连接池配置和当前活跃连接拉出来，我怀疑 RecentOrders 把池占满了",
             calls=[
                 C("Grep", {"pattern": "MaxOpenConns|MaxIdleConns|ConnMaxLifetime", "path": "internal/db/"},
                   generic_out("Grep", 8),
                   "确认上限是 40 之后，再看 pg_stat_activity 里这 40 个连接各自在等什么。",
                   "配置是 MaxOpenConns(40)、MaxIdleConns(20)、ConnMaxLifetime 30m，从 2 月那次扩容后没动过。"),
                 B("psql -h pg-order.internal -d orderdb -c \"select state, wait_event_type, left(query,60) from pg_stat_activity where datname='orderdb' group by 1,2,3 order by count(*) desc\"",
                   pg_stat(),
                   "38 个 active 全卡在同一条 SELECT，说明不是连接泄漏而是单条查询太慢把连接拖住了。",
                   "活跃连接 38 个全部停在 RecentOrders 的 SELECT 上，另外 2 个是 idle in transaction。池子不是泄漏，是被慢查询占住，后面的请求全在等连接。"),
             ]),
        dict(q="explain analyze 一下那条查询，顺便看看表上现有索引",
             calls=[
                 B("psql -h pg-order.internal -d orderdb -c \"EXPLAIN (ANALYZE, BUFFERS) SELECT id, user_id, sku_id, amount, status, created_at FROM orders WHERE user_id = '88910427' ORDER BY created_at DESC LIMIT 20\"",
                   explain(),
                   "重点看 Rows Removed by Filter 和 Buffers read，能直接算出多扫了多少页。",
                   "实锤了：Seq Scan 扫 481 万行、过滤掉 4812933 行，单条 713ms，Buffers read 91244。索引列表里只有主键、created_at、status，user_id 上没有索引，而且 last_analyze 还是 2 月 27 日。"),
                 Rf("migrations/20260302_add_orders_user_id_idx.sql",
                    "-- 线上建索引必须 CONCURRENTLY，避免长事务阻塞写入\n"
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS orders_user_id_created_at_idx\n"
                    "    ON orders (user_id, created_at DESC);",
                    "迁移文件已经写好了，接下来确认线上有没有执行过。",
                    "本地迁移文件里已经有 (user_id, created_at DESC) 的联合索引，用的是 CONCURRENTLY，是对的。查一下 prod 的迁移状态。"),
             ]),
        dict(q="这个索引线上到底跑了没有？我记得上周发过一次",
             calls=[
                 B("kubectl -n prod exec deploy/order-api -- ./migrate status",
                   "  Version   Description                          Applied At\n"
                   "  20260214  add_order_index                      2026-02-15 02:11:03 +00:00\n"
                   "  20260227  add_inventory_lock                   2026-02-28 01:47:52 +00:00\n"
                   "  20260302  add_orders_user_id_idx               PENDING\n"
                   "\n3 migrations, 2 applied, 1 pending",
                   "PENDING 说明上周那次发布没带上它，看看 git log 是不是被漏掉了。",
                   "没跑。migrate status 里 20260302 还是 PENDING，上周只发到 20260227。索引文件在主分支上，但发布清单漏了它。"),
                 B("git log --oneline -8 -- internal/checkout/ migrations/",
                   "\n".join(["%s %s" % (hid(7), m) for m in [
                       "fix(checkout): 幂等键校验前置", "perf(cache): sku 改批量查询",
                       "chore(migrations): 补 orders 联合索引", "fix(db): 连接池超时改为 3s",
                       "feat(checkout): 风控回查最近 20 单", "refactor(db): Store 接口收敛",
                       "test(checkout): 补并发用例", "chore: 升级 pgx 到 v5.5.4"]]),
                   "这几个提交里「补 orders 联合索引」和「sku 改批量查询」都在，正好对上性能拐点。",
                   "提交历史对上了：2 月底连着发了「风控回查最近 20 单」和「sku 改批量查询」，索引那条提交也在，但没进发布。所以是代码先上、索引没跟上。"),
             ]),
        dict(q="把这几个提交的 diff 贴出来，顺便看看重试逻辑有没有一起改",
             calls=[
                 B("git diff HEAD~3 -- internal/checkout/handler.go migrations/",
                   git_diff(),
                   "diff 里除了索引，还把逐个 sku.Get 换成了 BatchGet，这个改动要单独评估。",
                   "diff 有两处关键改动：一是 Checkout 加了幂等键校验，二是原来循环里逐个 sku.Get 换成 BatchGet。索引文件是新增的。BatchGet 目前没有合并并发请求，我担心压测时会击穿。"),
                 C("Grep", {"pattern": "retry|backoff|singleflight", "path": "internal/"},
                   "internal/payment/client.go:44:    backoff = []time.Duration{50*time.Millisecond, 200*time.Millisecond}\n"
                   "internal/payment/client.go:91:    for attempt := 0; attempt < 3; attempt++ {\n"
                   "internal/cache/lru.go:11:    // TODO: 合并并发 miss，避免热点 key 打到 DB\n"
                   "-- 3 matches in 2 files",
                   "lru.go 里那条 TODO 正好说明问题，重试逻辑只在 payment 侧，缓存这边没有保护。",
                   "重试只在 payment 客户端有 3 次退避；internal/cache/lru.go 第 11 行留了「合并并发 miss」的 TODO，一直没做。"),
             ]),
        dict(q="先在预发跑一遍压测，看索引加上之后 P99 能不能回到 200ms 以内",
             calls=[
                 B("k6 run --vus 80 --duration 3m scripts/checkout.js",
                   k6_out(),
                   "关注 p95 和 max 的差距，如果长尾还在，说明除索引外还有别的东西在排队。",
                   "预发加了索引后 p95 441ms、avg 186ms，比之前好很多，但 max 还有 2.31s，499 条检查失败。长尾没消掉，不是单纯索引的问题。"),
                 B("kubectl -n staging top pods -l app=order-api",
                   generic_out("Bash", 6),
                   "CPU 没打满、内存也稳，那长尾更像是协程阻塞，抓一下 pprof。",
                   "三个 Pod 的 CPU 都在 180m 左右、内存 300Mi，资源很空。既然不是 CPU 瓶颈，去看协程和锁。"),
             ]),
        dict(q="抓一下 goroutine 和锁的 profile，长尾应该在别的地方",
             calls=[
                 B("curl -sS 'http://127.0.0.1:6060/debug/pprof/goroutine?debug=1' | head -120",
                   pprof_out(),
                   "1678 个协程堵在 database/sql.(*DB).conn，说明请求还是在抢连接，只是成因变了。",
                   "goroutine profile 里 1678 个协程阻塞在 database/sql.(*DB).conn，pprof top 也显示 database/sql.(*DB).conn 累计 1.24s。连接争抢还在，而且 cache.(*LRU).Get 有 0.63s，和前面那个 TODO 对上了。"),
                 Rf("internal/cache/lru.go",
                    "internal/cache/lru.go\n"
                    " 54  func (l *LRU) Get(ctx context.Context, k string) (any, error) {\n"
                    " 55      l.mu.Lock()\n"
                    " 56      defer l.mu.Unlock()\n"
                    " 57      // TODO: 合并并发 miss，避免热点 key 打到 DB\n"
                    " 58      if v, ok := l.m[k]; ok {\n"
                    " 59          l.ll.MoveToFront(k)\n"
                    " 60          return v, nil\n"
                    " 61      }\n"
                    " 62      v, err := l.load(ctx, k)\n"
                    " 63      l.m[k] = v\n"
                    " 64      return v, err\n"
                    " 65  }",
                    "load 持着 mu 去查库，一个 miss 就把整张表锁住，热点 key 会串行化所有请求。",
                    "问题在这：Get 全程持互斥锁，第 62 行的 load 还在锁内查库。热点 key 一旦 miss，后来的请求全堵在 mu.Lock()，然后各自再去抢 DB 连接。这就是长尾。"),
             ]),
        dict(q="用 singleflight 合并并发 miss 吧，顺手把竞态也修了",
             calls=[
                 B("go test ./internal/cache/... -race -count=1 -run TestBatchGetConcurrent -v",
                   gotest(),
                   "-race 报的是 lru.go:58/63 的读写竞争，和刚才看到的锁内 load 是同一处。",
                   "复现了：TestBatchGetConcurrent 期望 loader 只调一次，实际 37 次，还有 12 个协程卡在 cache.mu；-race 报 lru.go:58 与 63 的数据竞争。先改实现再补单测。"),
                 C("Edit", {"file_path": "internal/cache/lru.go",
                            "old_string": "    if v, ok := l.m[k]; ok {",
                            "new_string": "    v, err, _ := l.group.Do(k, func() (any, error) {\n        return l.load(ctx, k)\n    })"},
                   "diff --git a/internal/cache/lru.go b/internal/cache/lru.go\n"
                   "@@ -54,11 +54,16 @@ func (l *LRU) Get(ctx context.Context, k string) (any, error) {\n"
                   "-    l.mu.Lock()\n-    defer l.mu.Unlock()\n"
                   "-    // TODO: 合并并发 miss，避免热点 key 打到 DB\n"
                   "+    l.mu.RLock()\n"
                   "     if v, ok := l.m[k]; ok {\n"
                   "+        l.mu.RUnlock()\n"
                   "         return v, nil\n"
                   "     }\n"
                   "+    l.mu.RUnlock()\n"
                   "+    v, err, _ := l.group.Do(k, func() (any, error) {\n"
                   "+        return l.load(ctx, k)\n"
                   "+    })\n"
                   "+    if err != nil { return nil, err }\n"
                   "+    l.mu.Lock(); l.m[k] = v; l.mu.Unlock()\n"
                   "     return v, err\n"
                   " }",
                   "改成 RWMutex + singleflight，读命中走读锁，miss 由 group.Do 合并，load 移到锁外。",
                   "改完了：命中路径用 RLock，miss 交给 singleflight 合并，load 挪到锁外，写回才短暂加写锁。现在同一个 key 只会有一个 loader 打到 DB。"),
             ]),
        dict(compact=True, q=cb_summary(), calls=[]),
        dict(q="继续，把 singleflight 的改动补上单测，然后跑全量",
             calls=[
                 B("go test ./... -race -count=1",
                   "ok      order-service/internal/checkout    1.913s  coverage: 87.4% of statements\n"
                   "ok      order-service/internal/db          0.887s  coverage: 79.1% of statements\n"
                   "ok      order-service/internal/cache       1.204s  coverage: 91.2% of statements\n"
                   "ok      order-service/internal/payment     0.542s  coverage: 83.6% of statements\n"
                   "ok      order-service/cmd/api              0.118s  coverage: 42.3% of statements\n"
                   "$ go vet ./...\n# 无输出",
                   "全绿了，再确认一下新增的并发用例覆盖的是「同一个 key」而不是不同 key。",
                   "全量 -race 和 go vet 都过了，cache 包覆盖率从 79% 提到 91.2%。新增用例是 100 个协程抢同一个 key，断言 loader 只执行一次。"),
                 Rf("internal/cache/lru_singleflight_test.go",
                    "  1  func TestBatchGetConcurrent(t *testing.T) {\n"
                    "  2      var loads atomic.Int32\n"
                    "  3      c := New(128, func(ctx context.Context, k string) (any, error) {\n"
                    "  4          loads.Add(1)\n"
                    "  5          time.Sleep(20 * time.Millisecond)\n"
                    "  6          return []byte(k), nil\n"
                    "  7      })\n"
                    "  8      var wg sync.WaitGroup\n"
                    "  9      for i := 0; i < 100; i++ {\n"
                    " 10          wg.Add(1)\n"
                    " 11          go func() { defer wg.Done(); _, _ = c.Get(ctx, \"sku:hot\") }()\n"
                    " 12      }\n"
                    " 13      wg.Wait()\n"
                    " 14      if got := loads.Load(); got != 1 {\n"
                    " 15          t.Fatalf(\"loader called %d times, want 1\", got)\n"
                    " 16      }\n"
                    " 17  }",
                    "用例是对的，接着看 CI 为什么挂。",
                    "用例没问题：100 个协程同时 Get 同一个 sku:hot，断言 loads==1。之前 CI 挂应该不是这处。"),
             ]),
        dict(q="CI 还是红的，看下流水线具体卡在哪一步",
             calls=[
                 Rf(".github/workflows/ci.yaml", ci_yaml(),
                    "流水线在 self-hosted kylin-v10 runner 上跑，先看是不是 runner 标签或 Go 版本的问题。",
                    "CI 用的是 self-hosted / kylin-v10 runner，Go 1.22。vet 在 race 之前，缓存竞争只有到 test 才暴露。"),
                 B("gh run view 18427 --log-failed | tail -40",
                   "test  Set up job  \u2713  0s\n"
                   "test  Run actions/checkout@v4  \u2713  2s\n"
                   "test  Run actions/setup-go@v5  \u2713  4s\n"
                   "test  vet  \u2713  11s\n"
                   "test  test  \u2717  38s\n"
                   "  --- FAIL: TestBatchGetConcurrent (0.12s)\n"
                   "      lru_test.go:118: expected single loader call, got 37\n"
                   "  FAIL\torder-service/internal/cache\n"
                   "test  build  -  skipped\n"
                   "test  image  -  skipped\n"
                   "ERROR: runner kylin-runner-03 lost heartbeat at 09:41:22Z",
                   "日志里的失败还是旧代码，runner 又掉了心跳，重跑一次确认。",
                   "这次失败日志是上一版代码（got 37），而且 kylin-runner-03 在 09:41 丢了心跳，build / image 直接跳过。不是代码问题，重跑一次。"),
             ]),
        dict(q="整理一下根因和上线步骤，我发给值班群",
             calls=[
                 B("git status --short && git diff --stat",
                   " M internal/cache/lru.go                         | 16 +++++++++-------\n"
                   " M internal/checkout/handler.go                 | 12 ++++++++----\n"
                   "?? internal/cache/lru_singleflight_test.go     | 17 +++++++++++++++++\n"
                   "?? migrations/20260302_add_orders_user_id_idx.sql | 3 +++\n"
                   " 4 files changed, 41 insertions(+), 11 deletions(-)",
                   "上线顺序要先索引后代码，CONCURRENTLY 建索引期间不能发版打断。",
                   "改动收敛到 4 个文件、+41/-11。上线分两步：先在 prod 用 CONCURRENTLY 建 (user_id, created_at DESC) 索引并观察 10 分钟，再发代码。索引没建完就发版，慢查询会把连接池再次打满。"),
                 B("git log --oneline -5",
                   "\n".join(["%s %s" % (hid(7), m) for m in [
                       "fix(cache): singleflight 合并并发 miss", "test(cache): 补 100 协程并发用例",
                       "perf(checkout): sku 批量查询", "chore(migrations): 补 orders 联合索引",
                       "fix(checkout): 幂等键校验前置"]]),
                   "把三条根因和两条上线动作按值班群能直接转发的格式写出来。",
                   "根因三条：① orders.user_id 缺索引导致 713ms 全表扫；② 连接池 40 个连接被慢查询占满，后续请求排队；③ lru.Get 在锁内 load，热点 key 击穿让长尾到 2.31s。上线两步：先 CONCURRENTLY 建索引，再发 singleflight + 幂等键，回滚只需回退代码、索引保留不影响写入。"),
             ]),
    ])

SIDE = [
    dict(sid=sid_of("idempotency-key"),
         day_off=3,
         title="为 checkout 补幂等键与重试测试",
         turns=[
             dict(q="支付回调偶发重复扣款，给 /v1/checkout 加个幂等键吧，Redis 存 24 小时",
                  calls=[
                      B("grep -rn 'idempot\\|Idempotency' internal/ | head -20",
                        "internal/middleware/request_id.go:22:    rid := r.Header.Get(\"X-Request-Id\")\n"
                        "internal/payment/client.go:88:    // 上游会重试 3 次，需要下游幂等\n"
                        "-- 2 matches in 2 files",
                        "现有代码里只有 request_id，没有幂等键，先定 key 的组成和过期策略。",
                        "目前只有 X-Request-Id，没有幂等语义。建议 key 用 uid + X-Idempotency-Key，Redis SET NX EX 86400，命中直接返回首次结果。"),
                      B("go test ./internal/checkout/... -run TestIdempotency -count=1 -v",
                        "=== RUN   TestIdempotencySameKey\n--- PASS: TestIdempotencySameKey (0.04s)\n"
                        "=== RUN   TestIdempotencyDifferentKey\n--- PASS: TestIdempotencyDifferentKey (0.03s)\n"
                        "=== RUN   TestIdempotencyConcurrent\n--- PASS: TestIdempotencyConcurrent (0.11s)\nPASS",
                        "并发用例过了，再确认 SET NX 的原子性有没有被上层缓存绕过。",
                        "三个用例都过了，包括 50 并发抢同一个 key 只扣款一次。"),
                  ]),
             dict(q="如果 Redis 挂了怎么办，别把下单整个堵死",
                  calls=[
                      C("Grep", {"pattern": "redis|fallback|degrade", "path": "internal/"},
                        "internal/cache/redis.go:31:    if err != nil { return err }\n"
                        "internal/cache/redis.go:47:    // TODO: 降级策略\n"
                        "-- 2 matches in 1 file",
                        "降级要 fail-open 但不能无限放，考虑本地 LRU 兜底短时间。",
                        "改成 fail-open：Redis 超时 50ms 就跳过幂等校验、打 metric 并告警，用本地 LRU 兜 30 秒。宁可极小概率重复，也不能让下单整体不可用。"),
                  ]),
         ]),
    dict(sid=sid_of("release-notes"),
         day_off=5,
         title="整理 v2.3.1 发布说明与回滚预案",
         turns=[
             dict(q="把这次的改动列成发布说明，再写个回滚预案",
                  calls=[
                      B("git log --oneline v2.3.0..HEAD",
                        "\n".join(["%s %s" % (hid(7), m) for m in [
                            "fix(cache): singleflight 合并并发 miss", "chore(migrations): 补 orders 联合索引",
                            "fix(checkout): 幂等键校验前置", "perf(checkout): sku 批量查询",
                            "test(cache): 补 100 协程并发用例"]]),
                        "5 个提交，按用户可见的变更和内部优化分开写。",
                        "5 个提交。发布说明分三块：性能（索引 + singleflight，P99 从 1.2s 回到 200ms 内）、稳定性（幂等键、Redis 降级）、内部（测试与覆盖率）。回滚预案写清楚索引保留、只回退镜像。"),
                  ]),
         ]),
]


# ---------------------------------------------------------------- 写 jsonl
def usage(n):
    """按 README 的峰档(2x)单价生成自洽的 token/积分。"""
    pin = 58_000 + n * 3_900 + (n * n * 7) % 5_300
    if n == 1:
        rate = 0.06                       # 会话首次调用，整段历史按全价重算
    elif n in (11, 27, 41):
        rate = 0.42 + (n % 3) * 0.03     # 缓存冷启动断点
    else:
        rate = 0.978 + (n % 7) * 0.0025  # 常态：97.8% ~ 99.3% 命中
    hit = int(pin * rate)
    out = 220 + (n * 37) % 900
    miss = pin - hit
    credit = (miss / 1000 * .0142857 + hit / 1000 * .000285714 + out / 1000 * .0571428) * 2
    return dict(prompt_tokens=pin, prompt_cache_hit_tokens=hit,
                prompt_cache_miss_tokens=miss, completion_tokens=out,
                total_tokens=pin + out, credit=round(credit, 4)), n + 1


def prov(n, final=False):
    u, nxt = usage(n)
    return ({"model": MODEL, "requestModelId": MODEL, "requestModelName": MODEL_NAME,
             "agent": "cli", "conversationRequestId": "req-" + hid(12),
             "rawUsage": u}, nxt)


def write_session(s, idx, counter):
    d = OUT / "projects" / PROJ_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / (s["sid"] + ".jsonl")
    ts = T0 + s.get("day_off", idx) * 86400_000 + idx * 3600_000
    rows = [{"type": "session-meta", "sessionId": s["sid"], "timestamp": ts,
             "cwd": CWD, "aiTitle": s["title"]}]
    n = counter
    for ti, turn in enumerate(s["turns"], 1):
        ts += 900
        up = {"type": "message", "role": "user", "timestamp": ts,
              "content": [{"type": "text", "text": turn["q"] + turn.get("inj", "")}]}
        if turn.get("compact"):
            up["providerData"] = {"agent": "cli", "isCompacted": True,
                                  "compactType": "auto", "isSummary": True}
        else:
            up["providerData"] = {"agent": "cli",
                                  "conversationRequestId": "req-" + hid(12)}
        rows.append(up)
        ts += 1100
        for ci, c in enumerate(turn["calls"], 1):
            pprov, n = prov(n)
            call_id = "call_%s_%d_%d" % (hid(8), ti, ci)
            rows.append({"type": "function_call", "name": c["tool"], "callId": call_id,
                         "arguments": json.dumps(c["args"], ensure_ascii=False),
                         "timestamp": ts, "providerData": pprov})
            ts += 850
            rows.append({"type": "reasoning", "timestamp": ts,
                         "content": [{"type": "text", "text": c["think"]}],
                         "providerData": {"agent": "cli"}})
            ts += 420
            rows.append({"type": "function_call_result", "name": c["tool"],
                         "callId": call_id, "timestamp": ts,
                         "output": {"text": c["out"]}})
            ts += 760
            aprov, n = prov(n)
            rows.append({"type": "message", "role": "assistant", "timestamp": ts,
                         "content": [{"type": "text", "text": c["say"]}],
                         "providerData": aprov})
            ts += 1400
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows), n



# ---------------------------------------------------------------- 外部源
# Codex / DSH 的演示数据写在输出目录下的 codex/ 与 dsh/，启动服务时用
# CODEX_HOME / DSH_HOME 指过去。这样截图里能看到平台切换真正在切数据，
# 而不是只画一个 WorkBuddy 的壳子。

def _fmt_ts(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def _zstd_frames(chunks):
    """多帧压缩：返回 None 表示本机没有可用的 zstd，跳过该源。"""
    try:
        from compression import zstd as _z            # Python 3.14+
        return b"".join(_z.compress(c.encode()) for c in chunks)
    except Exception:
        pass
    try:
        import subprocess
        return b"".join(subprocess.run(["zstd", "-q", "-c"], input=c.encode(),
                                       stdout=subprocess.PIPE, check=True).stdout
                        for c in chunks)
    except Exception:
        return None


CODEX_CWD = "/home/linwei/work/order-service"
DSH_CWD = "/home/linwei/work/order-service"


def write_codex(day_off=1):
    """一个 Codex rollout：session_meta → turn_context → 消息 / 工具 / usage。"""
    sid = str(uuid.uuid5(uuid.NAMESPACE_URL, "token-trace/demo/codex-index-check"))
    d = OUT / "codex" / "sessions" / "2026" / "01" / "01"
    d.mkdir(parents=True, exist_ok=True)
    ts = T0 + day_off * 86400_000 - 5400_000
    rows = [{"timestamp": ts, "type": "session_meta",
             "payload": {"session_id": sid, "cwd": CODEX_CWD,
                         "cli_version": "0.52.0", "model_provider": "openai"}},
            {"timestamp": ts, "type": "turn_context",
             "payload": {"model": "gpt-5.1-codex"}}]
    steps = [
        ("复核 orders 索引上线后 /checkout 的 P99，顺带看连接池水位",
         None, None, None, 0),
        (None, "shell", {"cmd": "psql -Atc \"select indexrelname, idx_scan from pg_stat_user_indexes where relname='orders'\""},
         "orders_user_id_idx|184203\norders_pkey|90211\n-- 2 rows", (86_000, 71_000, 320)),
        (None, "shell", {"cmd": "kubectl -n prod top pod -l app=order-api --containers"},
         "NAME                          CPU(cores)   MEMORY(bytes)\norder-api-7d9c4b8f-2xk4p   118m         412Mi\n"
         "order-api-7d9c4b8f-9vq7n   121m         409Mi", (128_000, 118_000, 410)),
        (None, "apply_patch", {"patch": "*** Begin Patch\n*** Update File: deploy/prod/values.yaml\n@@\n-  max_connections: 40\n+  max_connections: 80\n+  statement_timeout: 800ms\n*** End Patch"},
         "Done", (96_000, 90_000, 260)),
        (None, "shell", {"cmd": "kubectl -n prod rollout status deploy/order-api --timeout=180s"},
         "Waiting for deployment \"order-api\" rollout to finish: 2 of 6 updated replicas are available...\n"
         "deployment \"order-api\" successfully rolled out", (74_000, 70_000, 380)),
    ]
    for q, tool, inp, out, usage in steps:
        ts += 42_000
        if q:
            rows.append({"timestamp": ts, "type": "response_item",
                         "payload": {"type": "message", "role": "user",
                                     "content": [{"type": "input_text", "text": q}]}})
            continue
        rows.append({"timestamp": ts, "type": "response_item",
                     "payload": {"type": "message", "role": "assistant",
                                 "content": [{"type": "output_text",
                                              "text": "先看索引命中与连接数，再决定加不加池子。"}]}})
        ts += 900
        rows.append({"timestamp": ts, "type": "response_item",
                     "payload": {"type": "function_call", "name": tool,
                                 "arguments": json.dumps(inp, ensure_ascii=False)}})
        ts += 1200
        rows.append({"timestamp": ts, "type": "response_item",
                     "payload": {"type": "function_call_output", "name": tool, "output": out}})
        ts += 600
        pin, hit, pout = usage
        rows.append({"timestamp": ts, "type": "token_usage_record",
                     "payload": {"turn_id": "turn-%d" % (len(rows) // 4 + 1),
                                 "usage": {"input_tokens": pin, "cached_input_tokens": hit,
                                           "output_tokens": pout}}})
    path = d / ("rollout-%s-%s.jsonl" % (_fmt_ts(T0 + day_off * 86400_000), sid))
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _dsh_session(name, day_off, depth, turns):
    """turns: [(提问, [(工具名, 入参, 输出, (pin, hit, pout), 回答)])]"""
    sid = str(uuid.uuid5(uuid.NAMESPACE_URL, "token-trace/demo/dsh-" + name))
    d = OUT / "dsh" / "sessions" / "--home-linwei-work-order-service--" / ("session-" + sid)
    d.mkdir(parents=True, exist_ok=True)
    t = T0 + day_off * 86400_000 + 3_600_000
    ev = [{"type": "session", "version": 4, "id": sid, "createdAt": t,
           "cwd": DSH_CWD, "isSeeded": False, "delegationDepth": depth}]
    seq = 0
    for q, steps in turns:
        ev.append({"type": "turn/start", "seq": seq, "time": t, "data": {}}); seq += 1
        t += 800
        ev.append({"type": "user/message", "seq": seq, "time": t,
                   "data": {"content": [{"type": "text", "text": q}]}}); seq += 1
        for tool, inp, out, usage, say in steps:
            t += 1500
            ev.append({"type": "assistant/message", "seq": seq, "time": t,
                       "data": {"message": {"source": {"provider": "deepseek",
                                                       "model": "deepseek-v4.1-flash"},
                                            "content": [{"type": "text", "text": say}]},
                                "usage": {"inputTokens": usage[0], "cacheReadTokens": usage[1],
                                          "cacheWriteTokens": 0, "outputTokens": usage[2]}}})
            seq += 1
            t += 600
            ev.append({"type": "tool/call", "seq": seq, "time": t,
                       "data": {"name": tool, "input": inp}}); seq += 1
            t += 900
            ev.append({"type": "tool/result", "seq": seq, "time": t,
                       "data": {"content": out}}); seq += 1
    lines = [json.dumps(e, ensure_ascii=False) for e in ev]
    # 追加写 = 多个 zstd 帧，验证「只解首帧会丢内容」这条也被覆盖
    cut = max(1, len(lines) // 2)
    blob = _zstd_frames(["\n".join(lines[:cut]) + "\n", "\n".join(lines[cut:]) + "\n"])
    if blob is None:
        return None
    path = d / "session.v4.jsonl.zstd"
    with open(path, "wb") as f:
        f.write(blob)
    return path


def write_ext_sources():
    co = write_codex(day_off=1)
    d1 = _dsh_session("pool-check", day_off=2, depth=0, turns=[
        ("把连接池打满的那段时间拉出来，看看是哪些语句占着",
         [("bash", {"cmd": "grep -c 'pool_wait>500' /var/log/order-api/slow.log"},
           "318", (18_400, 96_000, 280),
           "慢查询日志里有 318 条等待超过 500ms，集中在 09:14–09:22。"),
          ("bash", {"cmd": "awk '{print $NF}' slow.log | sort | uniq -c | sort -rn | head"},
           "  211 db_wait\n   83 pool_wait\n   24 lock_wait\n-- 3 类", (9_800, 74_000, 260),
           "绝大多数是 db_wait，说明池子只是被慢查询占满，不是配置问题。"),
          ("bash", {"cmd": "psql -Atc \"select query, mean_exec_time from pg_stat_statements order by mean_exec_time desc limit 3\""},
           "select * from orders where user_id=$1|713.4\nupdate inventory set ...|182.9\nselect sku from ...|97.2",
           (12_600, 88_000, 300), "第一条全表扫，就是它在吃连接。")]),
        ("建索引的时候要不要停写",
         [("read", {"path": "migrations/0021_orders_user_idx.sql"},
           "CREATE INDEX CONCURRENTLY IF NOT EXISTS orders_user_id_idx ON orders (user_id);",
           (6_400, 52_000, 210),
           "用 CONCURRENTLY 就不阻塞写入，回滚只需 DROP INDEX，先把脚本补上。")]),
    ])
    d2 = _dsh_session("pool-verify", day_off=2, depth=1, turns=[
        ("并行核一下三台机器上的 max_connections 是不是都改了",
         [("bash", {"cmd": "for h in api-1 api-2 api-3; do ssh $h 'grep max_connections /etc/pgbouncer/pgbouncer.ini'; done"},
           "max_connections = 80 (api-1)\nmax_connections = 80 (api-2)\nmax_connections = 40 (api-3)",
           (7_200, 61_000, 240), "api-3 还是 40，配置没下发到那台。")]),
    ])
    return co, d1, d2


def main():
    (OUT / "projects").mkdir(parents=True, exist_ok=True)
    db = OUT / "workbuddy.db"
    con = sqlite3.connect(db)
    con.execute("drop table if exists sessions")
    con.execute("create table sessions (id text primary key, cwd text, title text, "
                "custom_title text, model text)")
    all_s = [MAIN] + SIDE
    for s in all_s:
        con.execute("insert into sessions values (?,?,?,?,?)",
                    (s["sid"], CWD, s["title"], "", MODEL))
    con.commit()
    con.close()
    total, n = 0, 1
    for i, s in enumerate(all_s):
        r, n = write_session(s, i, n)
        total += r
    ext = write_ext_sources()
    print("demo 数据: %s" % OUT)
    print("%d 个 WorkBuddy 会话 / %d 条记录 / %d 次带用量的模型调用" % (len(all_s), total, n - 1))
    print("外部源: Codex %s / DSH %s" %
          ("ok" if ext[0] else "跳过", "ok" if ext[1] else "跳过（无 zstd）"))
    print("主会话 id: %s" % MAIN["sid"])
    print("启动: WORKBUDDY_HOME=%s CODEX_HOME=%s/../codex DSH_HOME=%s/../dsh "
          "python3 -B trace_server.py --port 8917" % (OUT, OUT, OUT))


if __name__ == "__main__":
    main()
