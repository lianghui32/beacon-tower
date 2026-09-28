# 烽火台 Beacon Tower · 全栈可观测运维监控平台

**GitHub 仓库**：<https://github.com/lianghui32/beacon-tower>
**在线演示**：<https://beacon.lianghui.vip>（只读演示账号与密码直接显示在登录页；数据为演示种子 + 真实自采集）

[![CI](https://github.com/lianghui32/beacon-tower/actions/workflows/ci.yml/badge.svg)](https://github.com/lianghui32/beacon-tower/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Django](https://img.shields.io/badge/Django-5.2-44B78B?logo=django&logoColor=white)
![Tests](https://img.shields.io/badge/tests-119%20passing-2EA043)
![Docker](https://img.shields.io/badge/deploy-Docker%20Compose-2496ED?logo=docker&logoColor=white)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 烽火台是中国古代最早的"监控告警系统"——瞭望采集、狼烟告警、勤王响应。
> 本项目以同样的闭环，做一个**自包含**的 Django 全栈可观测平台（对标腾讯云可观测平台的模块形态）：
> 主机监控、应用性能监控 APM、前端性能监控 RUM、日志服务、告警中心、智能分析/数据挖掘、
> 自定义大盘、报表中心、接入中心、故障演练——全部数据**自采自析自存储**，不依赖任何外部 SaaS。
> 内置**整站登录认证**与**接入令牌**，支持通过自研 Agent 接入**多台服务器**。
> 开发端口：**8014**。

### 效果预览（截图取自线上实例）

| 监控总览 | APM 接口分析 |
|---|---|
| ![监控总览](docs/screenshots/dashboard.png) | ![APM](docs/screenshots/apm.png) |
| **调用链瀑布图** | **相关性分析矩阵** |
| ![调用链](docs/screenshots/trace.png) | ![相关性](docs/screenshots/analytics_corr.png) |
| **主机监控（psutil 真实读数）** | **日志查询** |
| ![主机](docs/screenshots/host.png) | ![日志](docs/screenshots/logs.png) |

## 一、项目背景（从"性能监控"到"可观测平台"）

本项目前身是一个 Django 性能监控与优化平台（内置问题论坛 + AST 诊断），解决的是
"Django 论坛 CPU 100% 找不到原因"这类单一问题。本次升级把它扩展成一个**完整的运维监控系统**，
覆盖可观测性的三大支柱（Metrics / Tracing / Logging）+ 前端真实用户监控（RUM）+ 告警闭环 + 智能分析：

- **数据收集**：请求计时中间件（含调用链 span）、psutil 主机采集线程、自研浏览器端 rum.js、
  日志 Handler 自动接入、OpenAPI 自定义指标/日志上报；
- **展示**：监控总览、各观测域分析页、可自定义的大盘；
- **分析**：慢请求/慢查询/错误聚合、调用链瀑布图、接口与 SQL 统计；
- **挖掘**：滑动窗口 3σ 异常检测、线性趋势预测、指标相关性分析（皮尔逊）、日志模板聚类挖掘；
- **优化**：保留原有的 AST 静态诊断引擎（N+1 / 全表加载等规则）+ 问题版/优化版对比压测；
- **告警闭环**：策略 -> 后台评估引擎 -> 事件 -> 通知记录，触发与恢复全流程。

## 二、功能全景（对照腾讯云可观测平台模块）

| 模块 | 路由 | 说明 |
|---|---|---|
| 监控总览 | `/` | 全局 KPI、请求/主机趋势、错误分布、服务健康表、触发中告警横幅，10 秒自动刷新 |
| 主机监控 | `/hosts/` | **psutil 真实采集**：CPU/负载/内存/磁盘/网络速率/进程数/TCP 连接数，15 秒/点 + Top 进程；**多主机下拉切换** |
| 多服务器接入 | `/api/ingest/host/` | 自研 Agent（`agent/obs_agent.py`，仅依赖 psutil）推送到平台，令牌保护，断网自动重试 |
| 登录认证 | `/accounts/login/` | 整站登录门禁（`AuthRequiredMiddleware`），未登录一律跳转；会话 Cookie HttpOnly + SameSite |
| 接入令牌 | `ingest_token.txt` | 所有上报端点（Agent/日志/指标/RUM beacon//metrics）要求"登录会话"或"令牌"，匿名写入 401 |
| 应用性能监控 APM | `/monitor/apm/` | 接口（事务）分析：量/耗时/P95/错误率/SQL；调用链列表 + **TraceID 瀑布图详情页**；支持 **W3C Trace Context**（上游服务携带 `traceparent` 即跨服务串联同一调用链，响应回写 `X-Trace-Id`/`traceresponse`） |
| 数据库分析 | `/monitor/apm/database/` | 各路径 SQL 统计 + 慢查询模板聚合（语句归并） |
| 前端性能监控 RUM | `/rum/…` | 自研 rum.js：数据总览 / 页面性能（TTFB·DOMReady·Load·FP·FCP·LCP 分位）/ 异常分析 / API 监控 / 静态资源 / 自定义上报 |
| 日志服务 | `/logs/` | logging Handler 自动入库（自动携带请求链路 ID）+ OpenAPI 推送；关键字检索、级别/来源/**trace_id** 过滤、级别直方图——日志与 APM 调用链按同一 ID 互查 |
| 拨测监控 | `/ops/probe/` | **黑盒监控**：URL 可用性/延迟/HTTPS 证书到期定时探测，probe.* 指标接入告警引擎 |
| 故障事件 | `/ops/incidents/` | 时间相近的告警自动聚合为故障单，时间线 + MTTR 统计 + 复盘报告（根因/改进措施）导出 |
| 巡检中心 | `/ops/inspection/` | 一键体检清单（资源水位/**磁盘内存耗尽预测**/接口质量/拨测证书/日志/SLO），健康评分，每 12 小时自动巡检 |
| SLO 错误预算 | `/ops/slo/` | 可用性/P95 目标 + 错误预算燃尽（预算烧穿提示冻结发布） |
| 资产台账 | `/ops/assets/` | Agent 接入的主机自动登记，维护负责人与环境（生产/测试）标签 |
| 自动处置 | `/ops/heal/` | 告警触发 → 白名单自愈动作（清理临时目录/HTTP 回调/参数列表命令），冷却期 + 执行留痕 |
| 通知渠道 | `/ops/notify/` | SMTP 真实邮件 / 企业微信 / 钉钉 / 通用 Webhook，配置页 + 测试发送 |
| 操作审计 | `/ops/audit/` | 策略变更 / 大盘编辑 / 自愈执行 / 登录成败全程留痕 |
| 清理加速 | `/cleaner/` | **磁盘空间分析**（目录/大文件 Top20，整盘可扫）、四类**垃圾清理**（系统临时文件 / `__pycache__` / pip 缓存 / 平台过期数据+VACUUM 真回收）、**内存整理**（工作集修剪，跳过系统进程）、Top 进程内存表，先预估后执行，全部留痕审计 |
| IP 访问地图 | `/analytics/geo/` | **中国/海外双地图切换**：省份热力 + 国家热力、省份/国家排行、24 小时时段分布、Top5 省份/国家×小时热力、Top20 IP 行为表（请求数/路径/峰值时段/错误率/归属地） |
| 告警中心 | `/alerts/…` | 策略 CRUD（任意注册表指标）/ 后台评估引擎（30 秒一轮）/ 事件（触发-恢复）/ 通知记录；支持 **for-duration 防抖**（持续越限 N 分钟才触发）、**恢复迟滞**（回到安全侧才恢复，防抖动反复）、静默窗口 |
| 智能分析 | `/analytics/` | 异常检测（滑动 3σ）/ 趋势预测（线性回归）/ 相关性矩阵（皮尔逊）/ 日志模式挖掘（模板聚类） |
| 性能诊断 | `/monitor/diagnose/` | 原 AST 静态扫描（R1 循环内查询/R2 未预取外键/R3 无 limit all()）+ 动态指标诊断，可导出 Markdown |
| 报表中心 | `/analytics/report/` | 日报/周报一键生成（请求/主机/前端/日志/告警汇总），可导出 Markdown |
| 自定义大盘 | `/monitor/dashboard/` | 从指标注册表选任意指标组成卡片网格（折线/面积/柱状，宽度和时间范围可调） |
| 接入中心 | `/integration/` | 六大观测域的接入方式与可直接复制的示例代码 |
| 故障演练 | `/forum/chaos/` | 一键制造慢请求 / N+1 / 500 错误 / 日志风暴 / CPU 空转，验证全链路联动 |
| Prometheus | `/metrics` | 自研 exposition 格式：APM + 主机 + RUM + 日志 + 告警 + 自定义指标全量输出 |
| 演示应用 | `/forum/problem/` `/forum/optimized/` | 故意埋了 N+1 / 无索引 / 全表加载 / 重复计算的论坛（问题版 vs 优化版） |

## 三、技术栈

| 类别 | 选型 | 备注 |
|---|---|---|
| 语言 | Python 3.10+ | |
| Web 框架 | Django 5.2 | 核心框架 |
| 数据库 | SQLite（演示零配置）/ PostgreSQL 16（生产） | `OBS_DATABASE_URL` 一键切换 |
| 应用服务器 | runserver（开发）/ gunicorn（生产） | Docker Compose 一键编排 |
| 测试 | Django TestCase × 52 | 算法/脱敏/限速/告警引擎/鉴权/追踪全覆盖，CI 自动执行 |
| CI | GitHub Actions | ruff 静态检查 + 系统检查 + migration 完整性 + 全量测试 |
| 主机采集 | psutil | 唯一推荐依赖；未安装自动降级为模拟指标 |
| 图表 | ECharts 5 | **本地自托管**（static/echarts.min.js），无 CDN 依赖 |
| 前端 SDK | 自研 rum.js | 纯原生 JS，零依赖，sendBeacon 批量上报 |
| 静态诊断 | Python 标准库 `ast` | 自研规则引擎 |
| 算法 | 纯标准库 | 滑动 3σ / 最小二乘 / 皮尔逊 / 正则模板聚类，可解释优先 |
| 后台线程 | threading | 主机采集 + 告警评估双线程（daemon，runserver 子进程内启动一次） |
| django-debug-toolbar / silk | 可选 | try-import，安装即用 |

## 四、快速开始

### 方式一：一键脚本（推荐）

```bash
bash start.sh
```

一条命令完成全部准备并启动：检测 Python（>=3.10）→ 创建虚拟环境 `.venv` → 安装依赖 →
数据库迁移 → 全新库自动初始化演示数据并创建管理员（凭据写入 `admin_credentials.txt`，
已有库自动跳过）→ 端口检查 → 启动服务 **http://127.0.0.1:8014**。脚本幂等，可放心重复运行。

| 命令 | 说明 |
|---|---|
| `bash start.sh` | 本地模式：一条命令从零到能跑，**无需 Docker** |
| `bash start.sh --setup-only` | 只做环境准备，不启动服务 |
| `bash start.sh --demo` | 补充初始化演示数据 |
| `bash start.sh --docker` | 生产编排：检查 Docker/Compose（缺失打印对应平台安装指引）→ 自动生成 `.env` 密钥 → `docker compose up --build` → 等健康检查 |

### 方式二：手动分步

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 建库并生成演示数据（论坛内容 + 六大观测域 24 小时数据 + 告警策略 + 大盘卡片）
python manage.py migrate
python manage.py init_data

# 3. 创建自己的登录账号（整站需要登录）
python manage.py createsuperuser

# 4. 启动（端口 8014，启动时自动拉起主机采集与告警评估线程）
python manage.py runserver 8014
```

打开 `http://127.0.0.1:8014/`，用上一步创建的账号登录即可。首次启动会在项目根目录生成
`ingest_token.txt`（接入令牌）与 `.secret_key`（Django 密钥），两者都是机密文件，不要提交到仓库
（已列入 `.gitignore`）。

### 生产部署（环境变量）

| 变量 | 说明 |
|---|---|
| `DJANGO_DEBUG=0` | 关闭调试页（默认 `1` 便于演示），并自动启用安全响应头 |
| `DJANGO_ALLOWED_HOSTS` | 逗号分隔的合法 Host，如 `obs.example.com,10.0.0.5` |
| `DJANGO_ALLOW_ALL_HOSTS=0` | 关闭默认的 `*` 兜底（演示用） |
| `OBS_INGEST_TOKEN` | 覆盖自动生成的接入令牌 |
| `OBS_WORKERS_ENABLED` | 强制指定/排除某进程参与后台任务（`1`/`0`）；不设时由租约选主决定谁干活 |
| `OBS_LEASE_TTL_SEC` | 后台任务租约时长（默认 60）：持有者宕机后其它副本最长等这么久接管 |
| `OBS_TRUST_XFORWARDED_FOR=1` | 仅在可信反向代理后开启，否则客户端可伪造 IP 污染统计与审计 |
| `OBS_HEAL_CMD_ALLOWLIST` | 自愈 command 白名单（可执行文件绝对路径，Windows 分号/Unix 冒号分隔），**不配置则 command 类型禁用** |
| `OBS_ALLOW_LOOPBACK_URL` / `OBS_ALLOW_PRIVATE_URL` | 出站拨测/回调允许环回/私网目标（默认允许，内网拨测需要） |
| `OBS_INSPECT_INTERVAL_HOURS` | 定时巡检周期（默认 12） |

## 五、接入要监控的服务器（多台）

平台所在机器默认自动采集；**其他服务器**在目标机器上跑自研 Agent 即可：

```bash
# 在目标服务器上
curl -o obs_agent.py http://平台IP:8014/static/agent/obs_agent.py
pip install psutil
export OBS_SERVER_URL="http://平台IP:8014"
export OBS_INGEST_TOKEN="<接入令牌，见接入中心页面>"
export OBS_HOST_NAME="web-1"        # 可选别名
python obs_agent.py                  # 或 nohup / systemd / Windows 计划任务常驻
```

Agent 每 15 秒推送一次指标到 `/api/ingest/host/`（令牌保护），断网自动重试；
打开"主机监控"页面，右上角下拉框切换查看各台服务器。"接入中心"页面有完整的
systemd / Windows 计划任务配置示例与安全建议（HTTPS、端口收敛、令牌轮换）。

## 六、推荐体验路径（10 分钟看完所有功能）

1. **监控总览** `/`：看 KPI、趋势与服务健康表；演练窗口的数据尖刺清晰可见；
2. **故障演练** `/forum/chaos/`：点"批量制造"，然后：
   - **APM** `/monitor/apm/` 看慢请求/错误率/调用链；
   - **日志查询** `/logs/` 看 ERROR 风暴落库（关键字搜"演练"）；
   - **主机监控** `/hosts/` 看 CPU 曲线抬升；
3. **告警中心** `/alerts/events/`：约 30 秒内评估引擎自动产生"触发中"事件与通知记录（也可在策略页点"立即评估"）；
4. **智能分析** `/analytics/`：对 `http.avg_duration` 跑异常检测（3σ 标注演练尖峰）、趋势预测、相关性矩阵、日志挖掘；
5. **前端 RUM**：访问 `/forum/problem/` 几次，再到 `/rum/` 看真实浏览器上报的 PV / LCP / JS 错误 / API 耗时；
6. **性能诊断** `/monitor/diagnose/`：AST 引擎自动找出 N+1 等问题并给出修复代码；
7. **基准压测** `/monitor/benchmark/`：问题版 vs 优化版量化对比；
8. **自定义大盘** `/monitor/dashboard/`：加两张自己的指标卡片；
9. **报表中心** `/analytics/report/`：生成日报并导出 Markdown；
10. **清理加速** `/cleaner/`（staff）：磁盘空间分析看"空间去哪了"，清理
    `__pycache__` / pip 缓存 / 系统临时文件，点"一键内存整理"看工作集修剪效果，
    再到"操作审计"看刚才的执行留痕。

## 七、数据采集架构（全部自研）

```
┌─ 浏览器 ─────────────┐   ┌─ Django 进程 ─────────────────────────────┐
│ rum.js（自研 SDK）    │   │ RequestTimingMiddleware                    │
│  PV/SPA路由           │   │  耗时/状态码/TraceID/SQL span/错误标记      │
│  TTFB·FCP·LCP         │──▶│  → RequestMetric（含 spans JSON 瀑布数据）  │
│  JS异常               │   │ hosts.collector 线程（15s）                 │
│  fetch/XHR 包装       │   │  psutil → HostMetric                       │
│  慢资源               │   │ loghub.SQLiteLogHandler（WARNING+）         │
└───────────┬──────────┘   │  → LogEntry                                │
            │ POST /rum/beacon/ │ alerts.engine 线程（30s）              │
┌───────────▼──────────┐   │  registry 均值 vs 阈值 → 事件+通知          │
│ 外部程序 / 脚本       │──▶│ OpenAPI：                                   │
│  POST /logs/api/ingest/  │  /api/ingest/metrics/ 自定义指标           │
│  POST /api/ingest/metrics/│ /logs/api/ingest/ 日志                    │
└──────────────────────┘   └────────────────────────────────────────────┘
```

**指标注册表**（`monitor/registry.py`）是全平台的取数枢纽：
18 个内置指标（HTTP/主机/RUM/日志）+ 动态自定义指标，每个 key 提供按分钟聚合的序列。
总览、大盘卡片、告警策略、智能分析全部从注册表取数——**新增一个指标只改一处**。

**采集不进同步写路径**：请求指标由中间件入内存队列（`monitor/buffer.py`），
后台线程按"攒满 200 条或满 1 秒"一次 `bulk_create` 批量落库——请求线程不再参与
SQLite 单写者锁竞争，实测 p99 从 2190ms 降到 251ms、QPS +23%（详见
[docs/BENCHMARK.md](docs/BENCHMARK.md)）。代价是指标可见延迟 ≤1 秒；
队列满时丢弃并计数，丢弃量经 `/metrics` 的 `obs_metric_buffer_dropped_total` 暴露，
进程正常退出前排空缓冲区。管理命令与测试里自动退回同步写库，保持"写完即可读"语义。

## 八、演示账号（向他人展示平台）

给评委/同事演示时不想暴露管理能力与接入令牌，用只读演示账号：

```bash
python manage.py create_demo_account          # 生成 demo 账号，凭据写入 demo_credentials.txt
python manage.py create_demo_account --username guest   # 自定义用户名
python manage.py create_demo_account --revoke --username demo   # 停用
```

创建后凭据会**直接展示在登录页**（"🎁 演示体验账号（只读）"蓝色提示框），
演示对象照着输入即可登录，无需口头转达；不需要展示时删除
`demo_credentials.txt` 或用 `--revoke` 停用账号即可。

凭据展示带三重安全校验：仅 DEBUG 模式展示（生产设 `DJANGO_DEBUG=0` 自动隐藏）、
只取文件中的第一组凭据、且该账号必须真实属于"演示访客"组——防止凭据文件被
误写入管理员账号时把超管密码公开在登录页。

演示账号属于"演示访客"组，登录后侧边栏出现"🎁 演示模式 · 只读"徽章：

- ✅ **可看**：监控总览、主机监控、APM（接口/调用链/数据库）、RUM 全部、日志查询、
  IP 访问地图、告警事件与通知记录、故障事件与复盘、巡检报告、SLO、资产台账、
  智能分析、报表中心、自定义大盘——高级功能全部可演示
- ❌ **不可看**：接入中心（含令牌明文）、通知渠道配置（含 SMTP 密码）、操作审计、
  自愈动作、清理加速中心、告警策略管理、拨测管理、故障演练、Prometheus 全量指标、后台
- ❌ **不可写**：所有 POST 变更（确认告警、巡检、大盘编辑…）与数据上报 API 一律 403

实现：`monitor/security.py` 的 `AuthRequiredMiddleware` 按组拦截；
侧边栏经 `monitor/context_processors.py` 注入的 `is_demo` 标记自动裁剪入口。

## 九、告警引擎说明

- 策略：`指标 key + 比较符 + 阈值 + 级别(P0/P1/P2/提示)`；
- **for-duration 防抖**：策略可设"持续 N 分钟"——指标越限后进入观察期，
  持续满 N 分钟才真正触发，瞬时毛刺不再产生告警（观察期内条件解除自动重置计时）；
- **恢复迟滞（hysteresis）**：策略可设"恢复阈值"——指标回到安全侧才算恢复，
  在触发阈值附近抖动的指标不会反复触发/恢复（防 flapping）；
- **静默窗口**：发版/维护期间一键静默 N 小时，评估引擎跳过；
- 评估：后台线程每 30 秒一轮，取最近 5 分钟窗口内均值与阈值比较；
- **跨进程只有一个评估者**：告警/采集/拨测/巡检四个任务各持一把租约
  （`monitor/leadership.py`，抢约是单条 compare-and-swap 条件更新），
  worker 多副本部署时其余副本热待命，持有者宕机后 TTL 内自动接管；
  进程内另有 `evaluate_lock` 防"后台线程 + 页面立即评估"并发产生重复事件；
- 事件：条件成立且无未恢复事件 → 新建 firing 事件 + 站内信/模拟 Webhook 通知；
  条件解除 → 标记 resolved + 恢复通知；并发评估产生的重复事件会自愈合并；
- 内置 7 条演示策略（含一条业务自定义指标"退款队列积压"）。

## 十、智能分析算法（可解释优先）

| 能力 | 算法 | 输出 |
|---|---|---|
| 异常检测 | 滑动窗口 3σ（窗口 20 分钟） | 图上标注异常点 + 偏离 σ 表格 |
| 趋势预测 | 最小二乘线性回归 + R² | 未来 30/60/120 分钟外推曲线 |
| 相关性分析 | 皮尔逊相关系数矩阵 | 7 个核心指标热力图 + 显著相关对解读 |
| 日志挖掘 | 数字/ID/IP/路径归一化为占位符后聚类 | 错误模板 Top N + 级别分布 + 示例 |

## IP 归属地解析（可插拔）

请求中间件在采集时解析 `client_ip` 的归属地（省份/城市，存入 `geo_province/geo_city`），
解析器按优先级自动选择（`monitor/geoip.py`）：

1. **GeoLite2**：`pip install geoip2`，把 `GeoLite2-City.mmdb` 放到项目根目录；
2. **纯真库**：`pip install qqwry-py3`，把 `qqwry.dat` 放到项目根目录；
3. **内置演示解析**：两个库都不存在时，按 IP 哈希确定性映射到省份/省会，
   并按约 8.5% 权重生成海外访客（国家名与 ECharts world.json 对齐）。
   同一 IP 结果恒定，仅供演示——生产请接入上两种之一。

解析结果带进程内缓存；"IP 访问地图"页面标题栏会显示当前生效的解析后端。

## 十一、Prometheus 对接（可选）

`/metrics` 覆盖：`django_requests_*`、`django_request_errors_*`、`django_sql_queries_*`、
`django_slow_queries_*`、`host_cpu_percent`、`host_memory_percent`、`rum_pageviews_total`、
`rum_js_errors_total`、`log_errors_total`、`alert_events_firing`、`custom_metric_*` 等。

```yaml
scrape_configs:
  - job_name: 'observability'
    scrape_interval: 15s
    static_configs:
      - targets: ['127.0.0.1:8014']
    metrics_path: '/metrics'
```

不部署 Prometheus 也完全不影响平台——自身面板独立可用。

## 十二、项目结构

```
14_Django性能监控与优化平台/
├── manage.py
├── requirements.txt
├── config/                     # settings / urls（含旧路径兼容重定向）
├── static/
│   ├── echarts.min.js          # 本地自托管，无 CDN 依赖
│   ├── rum.js                  # 自研前端监控 SDK（零依赖）
│   └── obs/common.js           # 平台页面公共工具（图表/时间范围/轮询）
├── forum/                      # 演示目标应用（故意含性能问题 + 故障演练端点）
├── monitor/                    # 平台核心
│   ├── middleware.py           #   请求计时 + TraceID + SQL span + 错误捕获
│   ├── models.py               #   RequestMetric(调用链) / CustomMetric / DashCard / TaskLease
│   ├── tracing.py              #   W3C Trace Context（traceparent 校验与 contextvars 绑定）
│   ├── buffer.py               #   请求指标批量缓冲（采集与写库解耦 + 丢弃计数）
│   ├── leadership.py           #   后台任务租约选主（CAS 抢约 + LeaseLoop 骨架）
│   ├── security.py             #   整站门禁 / 令牌作用域 / 限速 / SQL 脱敏
│   ├── registry.py             #   ★ 指标注册表（全平台统一取数入口）
│   ├── services.py             #   总览/APM/数据库聚合 + 压测
│   ├── metrics.py              #   Prometheus 文本输出（含采集管道与租约自观测）
│   ├── diagnoser.py            #   AST 静态诊断引擎（核心创新保留）
│   ├── workers.py              #   后台线程启动器（幂等）
│   ├── tests.py                #   链路/门禁/缓冲/租约回归（52+ 例）
│   └── management/commands/
│       ├── init_data.py        #   全观测域演示数据种子
│       ├── obs_workers.py      #   worker 角色（副本数不限，内部选主）
│       └── run_benchmark.py    #   压测命令
├── hosts/                      # 主机监控（psutil 采集线程 + 页面 + 远程上报）
├── rum/                        # 前端性能监控（beacon + 6 个分析页）
├── loghub/                     # 日志服务（Handler + 接入 API + 查询页）
├── alerts/                     # 告警中心（策略/引擎/事件/通知 + 引擎回归测试）
├── analytics/                  # 智能分析（算法库 + 工作台 + 报表 + IP 访问地图）
├── ops/                        # 运维中心（拨测/故障单/巡检/SLO/资产/自愈/通知渠道/审计）
│   ├── urlsafe.py              #   出站 URL 统一校验（SSRF 闸门 + 禁重定向）
│   ├── crypto.py               #   SMTP 授权码 Fernet 加密落库
│   ├── probing.py              #   拨测引擎（可用性/延迟/证书）
│   ├── incidents.py            #   告警聚合为故障单 + 复盘导出
│   ├── inspection.py           #   巡检清单引擎 + 容量耗尽预测
│   ├── heal.py                 #   白名单自愈执行器（拒绝 .bat + 冷却期原子抢占）
│   └── tests.py                #   上述安全路径回归测试
├── cleaner/                    # 清理加速中心（磁盘分析 / 垃圾清理 / 内存整理）
│   ├── services.py             #   白名单清理项 + Junction 剪枝 + 有界扫描
│   └── tests.py                #   "删除越界"逃逸测试
└── templates/                  # base（侧边栏布局）+ 各模块页面
```

## 十三、创新点

1. **一个注册表打通全平台**：指标注册表让大盘、告警、异常检测、预测、相关性分析共享同一套取数逻辑，新增指标零成本接入所有功能。
2. **全链路自研采集**：请求中间件（含调用链 span）、psutil 采集线程、浏览器 SDK、日志 Handler 四路数据全部自研，无任何外部 APM/SAAS 依赖。
3. **调用链可视化**：每个请求自动生成 TraceID 与 SQL/视图 span，ECharts 自定义 renderItem 绘制瀑布图。
4. **挖掘算法可解释**：3σ 给出偏离倍数、预测给出斜率与 R²、相关性给出系数与方向解读、日志挖掘给出归并模板与示例——每个结论都能回溯到原始数据。
5. **"制造故障 → 观察反应"的闭环演示**：故障演练页一键生成慢请求/错误/日志风暴/CPU 抬升，30 秒内可在 APM、日志、主机、告警四个模块看到联动反应，把抽象的"可观测性"变成可操作的实验。
6. **问题版/优化版同框对比**（继承自前身项目）：同一论坛页面的两种实现，页面底部实时显示 SQL 次数与耗时，配合压测输出量化对比。
7. **运维闭环延伸到"机器本身"**：巡检给出健康评分与容量耗尽预测，自愈执行白名单处置，清理加速中心做磁盘分析/垃圾清理/内存整理——监控不止于"看"，还能"治"。
8. **安全工程贯穿全程**：四类对手威胁模型 + 三轮红队审查闭环（每轮发现→修复→回归实测），安全白皮书（SECURITY.md）记录全部防护设计与验证方法，可复现、可审计。
9. **工程化成熟度**：119 个测试用例 + GitHub Actions CI（lint/检查/migration 完整性/测试）、
   Docker Compose 三角色生产编排（web/worker/db 分离）、后台任务租约选主（多副本只跑一份，持有者宕机自动接管）、
   采集写路径与请求路径解耦（批量缓冲 + 丢弃计数自观测 + 退出排空）、
   W3C Trace Context 跨服务链路语义、告警 for-duration/恢复迟滞/静默等生产语义、
   有真实压测数据的性能基准报告。

## 十四、安全设计（自研防护清单）

> 完整的安全白皮书（威胁模型、三轮红队审查记录、验证方法、生产加固清单、
> 已知权衡）见 **[SECURITY.md](SECURITY.md)**，以下为速览。

- **整站登录门禁**：未登录访问任何页面一律跳转登录页；API 路径返回 401 JSON；
  `/api/health/` 为唯一公开探活端点（仅返回 ok + 时间）；
- **接入令牌最小作用域**：令牌只放行**写数据的上报端点**（`/api/ingest/`、
  `/logs/api/ingest/`、`/rum/beacon/`）与 `/metrics`，**读类数据 API（日志检索/导出、
  APM 查询、主机序列）与所有页面一律只认登录会话**；比较使用
  `secrets.compare_digest`（防时序侧信道）；推荐一律用请求头传递，
  `?token=` 查询串仅为兼容旧 Agent 保留（会进访问日志，不推荐）；
- **上报 API 输入防御**：数值字段校验 NaN/Infinity/超界（防 SQLite 溢出 500）、
  日志级别白名单、单批条数上限、payload 大小限制（413）；
- **速率限制**：上报端点按客户端 IP 限速（120 次/分钟，超限 429），
  手动采样 20 次/分钟；登录按"用户名 10 次 + IP 30 次 / 10 分钟"计数——
  IP 超限直接 429（攻击者自锁），用户名超限后**错密码 429、正确密码始终放行**
  （防止攻击者拿用户名把受害者锁在门外），成功登录自动清零；
- **清理加速安全边界**：仅 staff 可用；遍历与清理**剪枝符号链接与 Windows
  Junction**（防挂载点把删除引出白名单根）；清理/VACUUM/内存整理进程级互斥
  （防重复 VACUUM 持写锁阻塞在线请求）；磁盘扫描 25s 时限 + 6 次/分钟限速；
- **出站请求 SSRF 防护**：拨测 / 自愈回调 / Webhook 统一经 `ops/urlsafe.py` 校验
  （http/https、逐 IP 阻断链路本地/云元数据/保留地址），且**禁止跟随重定向**
  （防外网跳板 302 打内网）；拨测的创建/启停/手动执行仅 staff 可操作
  （防止借平台对内网做端口/内容探测）；
- **自愈命令白名单**：`command` 类型必须先配置 `OBS_HEAL_CMD_ALLOWLIST`
  登记可执行文件绝对路径，否则整体禁用；**拒绝 .bat/.cmd**（Windows 下会经
  cmd.exe 重新解析参数，存在注入风险）；`shell=False` + 参数列表执行；
  自愈 / 通知渠道 / 审计页仅对 staff 账号开放；
- **敏感配置加密**：SMTP 授权码以 Fernet 密文落库（`ops/crypto.py`，SECRET_KEY 派生密钥），
  页面不回显；接入中心对非 staff 账号掩码显示令牌；DEBUG 报错页对
  令牌/密码所在视图启用 `sensitive_variables`/`sensitive_post_parameters`；
- **登录页凭据展示三重校验**：仅 DEBUG 模式、只取文件第一组凭据、
  且该账号必须真实存在并属于"演示访客"组（防止凭据文件被误写入管理员账号）；
- **密码策略**：创建/改密强制长度 ≥8 + 常见弱口令拒绝（`AUTH_PASSWORD_VALIDATORS`）；
- **XSS 防护**：调用链/数据库分析页的动态 JSON 一律 `json_script` 安全嵌入；
  日志内容 / RUM 上报 / 自定义指标名渲染前经 `esc()` 转义；ECharts tooltip 统一转义；
- **CSRF**：全部 POST 表单带 token，演练端点 / "立即评估" 等 fetch POST 显式携带；
  演练端点为 POST-only + 15 秒节流；
- **SQL 慢查询脱敏**：落库前对 password/passwd/pwd/secret/token/credential/api_key/auth
  等关键词的字面量（含 `_hash`/`_key` 后缀与 LIKE/IN 形式）做掩码；
- **审计可信**：审计 IP 默认取 REMOTE_ADDR（XFF 仅在可信反代开关后使用），
  审计写入失败记录日志而非静默；
- **CSV 导出防公式注入**：`= - + @` 开头的单元格加前缀阻断（日志导出 / APM 导出）；
- **演示账号**：只读组 + 写保护 + 敏感页拦截；`create_demo_account` 拒绝重置
  管理员账号（防止降权劫持）。

## 十五、生产部署（Docker Compose）

web 与后台任务分离的三角色编排（`docker-compose.yml`）：

| 角色 | 内容 | 副本数 |
|---|---|---|
| `db` | PostgreSQL 16（指标/日志/告警持久层） | 1 |
| `web` | gunicorn（2 worker × 4 线程）只处理请求，`OBS_DISABLE_WORKERS=1` | 可多副本 |
| `worker` | `python manage.py obs_workers`：采集/告警/拨测/巡检 | **可多副本**（租约选主） |

四个周期任务各自有一把租约（`monitor/leadership.py` + `monitor_tasklease` 表）：
抢约是一条 compare-and-swap 条件更新，同一任务全集群只有一个进程执行，其余副本热待命；
持有者被杀或宕机时租约到期，由其它副本自动接管。
实测（TTL=20s、三副本）：硬杀持有者后 28 秒内待命副本接管（term 1→2），
期间主机采集仍严格每 15 秒一行，没有重复采集。

```bash
cp .env.example .env            # 填入 SECRET_KEY / 接入令牌 / PG 密码
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
docker compose up -d --scale worker=3     # 可选：后台任务也要高可用
```

要点：
- 数据库用环境变量 `OBS_DATABASE_URL`（`postgres://user:pass@host:5432/db`）切换，SQLite 仅作开发兜底；
- 采集/评估线程从 web 进程剥离为独立 worker 角色——扩容 web 副本不会产生重复采集/重复告警；
- 接管延迟上界 ≈ `OBS_LEASE_TTL_SEC`（默认 60 秒；上面实测用 20 秒是为了缩短观察窗口）。
  租约是租约不是围栏（fencing）：旧持有者若正跑着一轮长任务，仍可能把那一轮做完，
  所以四个任务都写成幂等（告警按策略去重合并、采集点最多多一条）；
- 多机部署 worker 副本要求各节点时钟同步（NTP），否则过期判定随时钟漂移偏差；
- 容器以非 root 用户运行；`SECRET_KEY` / 接入令牌 / 演示账号凭据 / SQLite 兜底库都在
  命名卷 `obsdata:/data` 里（不挂卷的话容器一重建密钥就重新生成，会话与已加密的
  SMTP 授权码全部失效）；镜像内不含任何密钥（`.dockerignore` 排除全部凭据文件）；
- 静态文件：`collectstatic` 产物默认在 `./staticfiles`，由宿主机 nginx 直接伺服
  `/static/`——`DEBUG=0` 时 Django 不处理静态文件，只反代应用会让页面丢样式与图表；
  代码树对服务用户只读的部署（systemd + root 拥有的仓库目录）用
  `OBS_STATIC_ROOT` 指到可写目录，并让 nginx 的 `alias` 指向同一处；
- 对外演示想让访客在登录页直接看到只读演示账号，设 `OBS_SHOW_DEMO_ACCOUNT=1`
  （默认跟随 `DEBUG`，生产不主动外泄）；凭据文件写在 `OBS_DATA_DIR` 指向的目录；
- 反向代理场景还需在 `.env` 里显式打开 `OBS_TRUST_XFORWARDED_FOR=1`（否则客户端 IP
  一律取连接地址，地域统计失真）与 `DJANGO_SECURE_COOKIES=1` + `DJANGO_CSRF_TRUSTED_ORIGINS`
  （HTTPS 下 Cookie 只走加密通道并开 HSTS）；两者默认关闭，纯本机 http 演示不要开；
- 健康检查：web 容器对 `/api/health/` 探活，db 用 `pg_isready`；
- web 端口默认只绑宿主机 `127.0.0.1:8014`（反代在本机转发），公网不必直连应用端口；
  确需直连时设 `OBS_PUBLISH_ADDR=0.0.0.0`。

### 无 Docker 的 systemd 形态（本仓库线上实例用的就是这个）

线上那台机器上已经有宿主 PostgreSQL 与 nginx、且没装 Docker，再塞一套容器只是白占内存，
所以同一份代码也支持直接 systemd 托管：

```bash
python3 -m venv /opt/beacon-tower/.venv && .venv/bin/pip install -r requirements-prod.txt
# /etc/beacon.env 里放 DJANGO_* 与 OBS_* 环境变量（EnvironmentFile 格式）
# 两个单元：gunicorn 对外 127.0.0.1:8014；另一个跑 python manage.py obs_workers
systemctl enable --now beacon.service beacon-worker.service
```

要点：
- **gunicorn 用单进程多线程**（`--workers 1 --threads 4`）而不是多进程：限速与登录锁定
  目前是进程内 cache，开 N 个 worker 等于把限额放大 N 倍。要横向扩就先换 Redis cache，
  而不是先加 worker；
- 后台任务仍由 `obs_workers` 单独一个进程承担，与 web 分离；多副本时租约选主照样生效；
- 代码树归 root、服务用户只读时，`OBS_STATIC_ROOT` 与 `OBS_DATA_DIR` 必须指到可写目录
  （静态文件由 nginx 伺服，见上文）；
- 走 Cloudflare 橙云时，nginx 要配 `set_real_ip_from` + `real_ip_header X-Forwarded-For`，
  应用侧设 `OBS_TRUST_XFORWARDED_FOR=1`；两者缺一，访客就全被记成 CF 出口 IP，
  按 IP 的限速会大面积误伤，IP 地域分析也就没有意义了。源站证书用 CF 的 Origin CA
  （SSL/TLS 模式 Full strict），私钥在源站本地生成、不经外部传递。

## 十六、测试与 CI

```bash
python manage.py test          # 119 个用例，SQLite 内存库，无需外部服务
python -m ruff check .         # 静态检查（ruff.toml）
```

覆盖范围：
- **算法库**：3σ 异常检测（含零方差基线边界）/ 线性预测 / 皮尔逊 / 日志模板聚类；
- **告警引擎**：触发与去重、恢复、静默、for-duration 防抖（毛刺不触发、持续越限才触发、
  观察期重置）、恢复迟滞（阈值间抖动不反复）、重复事件合并自愈；
- **安全边界**：令牌三种传递方式与坏令牌 401、读类 API 令牌越权拦截、
  上报接口限速 429（不同 IP 独立计数）、SQL 脱敏（含普通列名不误伤）、NaN/Infinity 丢弃；
- **链路追踪**：traceparent 合法解析与 8 类非法输入拒绝、上游 trace 采纳/无头生成、
  请求上下文日志自动携带 trace_id、按 trace_id 检索日志；
- **采集缓冲**：入队不入库、flush 后入库、200 条批量无丢行、采集时刻不被落库时刻覆盖、
  队列满丢弃并计数、缓冲关闭退化为同步写、后台线程自动按批落库、
  退出前排空（用 30 秒刷新周期把这条承诺钉成回归测试）；
- **租约选主**：独占抢约、续约不换任期、过期可接管且任期 +1、被接管的原持有者必须认输、
  交还后可立即接管、非持有者不执行任务、持有者失效后待命副本接管、
  任务异常不丢租约也不带走循环、work 返回值即下轮等待秒数；
- **删除与执行边界**（清理中心是全平台唯一真删文件的地方，风险不对称）：
  Junction/符号链接剪枝——含"伪装成 Junction 能否把删除引到白名单外"的逃逸用例、
  遍历限时限量、只删超龄期文件、未知清理项一律拒绝、VACUUM 的 autocommit 约束；
  自愈命令白名单（未登记即整体禁用、拒绝 .bat/.cmd、参数里的 `& echo` 不被 shell 解释、
  30 秒超时）、`cleanup_tmp` 只允许系统临时目录之下、冷却期并发原子抢占；
- **凭据与出站**：SMTP 授权码密文落库（换密钥时优雅返回空而不是炸链路）、
  SSRF 闸门（协议/URL 内凭据/169.254.169.254 等元数据地址/保留与组播/
  环回与私网开关/IPv6 段/DNS 解析结果复核/禁跟随 302）。

GitHub Actions（`.github/workflows/ci.yml`）每次推送执行：
ruff 检查 → `manage.py check` → migration 完整性（`makemigrations --check`）→ 全量测试。

## 十七、性能基准

读路径（公开探活）**1266 QPS / p99 43ms / 零错误**；写路径（令牌上报落库）
**445 QPS / p99 787ms / 零错误**（SQLite 单写者锁长尾，PostgreSQL 形态可消除）；
限速器在真实并发下实测可靠触发 429。

业务页面（走 APM 采集，16 线程闭环）逐条同步写 → 批量缓冲写的对照实测：
**QPS 74→91，p95 958→214ms，p99 2190→251ms，max 5499→644ms，零错误**。
其中"p50 从 92ms 抬到 176ms"是闭环压测的排队假象而非服务退化，
报告里用 均值=并发/QPS 的对账拆清了这一点——改造后 p50≈均值，
说明延迟来源已从 I/O 争抢变回单进程 CPU 串行。方法、五组数据、
缓冲区丢点上界的行数对账与复现步骤见 [docs/BENCHMARK.md](docs/BENCHMARK.md)。

## 十八、说明与边界

- 主机指标为真实 psutil 读数；请求级 `cpu_percent` 为耗时/SQL 推算的演示指标。
- 请求指标经批量缓冲落库，面板/告警上看到的新数据最多滞后 1 秒（可调
  `OBS_METRIC_BUFFER_FLUSH_SEC`）；进程被强杀时最多丢失一个批次（≤200 条）的采集点，
  丢失数在 `/metrics` 里可见。
- 内存整理（清理加速）仅 Windows 支持（工作集修剪）；类 Unix 由内核自主管理页缓存。
- 清理加速只触碰白名单目标（临时目录/`__pycache__`/pip 缓存/平台过期数据），
  不提供任意目录删除——运维级工具宁可少删不可误删。
- 告警通知为站内信 + 模拟 Webhook，邮件/企业微信/钉钉为真实发送（需在通知渠道页配置）。
- 采集数据保留 7 天（后台线程分批清理）；审计日志保留 90 天；巡检报告保留 60 份。
- SQLite（WAL 模式）+ 单机线程面向教学演示；生产部署换 PostgreSQL + gunicorn +
  独立 worker 角色（`docker compose up` 即得该形态，见第十五节）。
- 静态诊断规则为保守检测，报告中的"疑似"结论需结合业务确认。
- 上报接口（beacon / 日志 / 自定义指标）要求登录会话或接入令牌；beacon 依赖登录
  Cookie，收集匿名公网访客需自行增加轻量鉴权方案。
