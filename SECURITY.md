# 安全白皮书（SECURITY.md）

> 本文档面向部署者、评审者与后续维护者，完整记录本平台的安全设计、三轮红队
> 审查结论、验证方法与已知权衡。安全相关改动请同步更新本文档。

## 一、威胁模型

平台按四类对手设计防线（能力由弱到强）：

| 对手 | 能力 | 主要防线 |
|---|---|---|
| **A. 匿名访问者** | 可访问 8014 端口，无账号无令牌 | 整站登录门禁、令牌校验、限速、公开面收敛到 3 个端点 |
| **B. 令牌持有者** | 拿到某台 Agent 机器上的接入令牌 | 令牌最小作用域（只能写上报端点 + 抓 /metrics）、限速、输入钳制 |
| **C. 演示访客** | 持演示组账号，可登录看面板 | 只读中间件（写操作 403）+ 敏感页前缀拦截 |
| **D. 普通登录用户** | 非 staff 账号 | 高危功能（自愈/通知/审计/清理/拨测写操作/完整令牌）staff 专属 |

平台**不设防**的对手：已获得服务器shell的攻击者（能读 `db.sqlite3` 与
`ingest_token.txt` 即等于全盘接管——这是任何自托管系统的共同边界）。

## 二、安全架构（分层）

```
请求 ──▶ SecurityMiddleware
     ──▶ Session/CSRF/Auth 中间件
     ──▶ AuthRequiredMiddleware        ① 整站门禁 + 令牌最小作用域（monitor/security.py）
     ──▶ RequestTimingMiddleware       ② 采集脱敏（SQL 字面量掩码、XFF 开关）
     ──▶ 视图层                         ③ require_ingest / _staff_required / 限速装饰器
     ──▶ 模板层                         ④ json_script / esc() / ECharts tooltip 转义
出站请求 ──▶ ops/urlsafe.py            ⑤ SSRF 校验 + 禁止重定向
执行动作 ──▶ ops/heal.py 等            ⑥ 白名单 + 互斥 + 留痕审计
```

## 三、防护清单（按主题）

### 1. 认证与会话
- 整站登录门禁：未登录访问页面一律 302 登录页；API 路径返回 401 JSON
  （`monitor/security.py`）。公开面仅 3 个：登录页、静态资源、`/api/health/`
  （探活端点只返回 ok + 时间，无敏感信息）。
- 接入令牌**最小作用域**：仅放行 `/api/ingest/`、`/logs/api/ingest/`、
  `/rum/beacon/` 与精确 `/metrics`——**读类数据 API（日志检索/导出、APM 查询、
  主机序列）与全部页面一律只认登录会话**。令牌分发在多台 Agent 上，泄露半径
  被限制为"只能写监控数据"。比较统一使用 `secrets.compare_digest`（防时序侧信道）；
  `?token=` 查询串仅为旧 Agent 兼容保留（推荐请求头，避免进入访问日志/Referer）。
- 登录暴力破解防护：按"用户名 10 次 + IP 30 次 / 10 分钟"计数（`ops/audit.py`
  登录失败信号）。**IP 超限直接 429（攻击者自锁）；用户名超限后错密码 429、
  正确密码始终放行并自动清零**——防止攻击者拿用户名把受害者锁在门外（DoS）。
- 会话 Cookie：HttpOnly + SameSite=Lax，14 天有效期；DEBUG=0 可叠加
  `DJANGO_SECURE_COOKIES=1` 启用 Secure + HSTS。
- 密码策略：≥8 位 + 常见弱口令拒绝（`AUTH_PASSWORD_VALIDATORS`）。
- `create_demo_account` 拒绝重置 staff/超管账号（防止降权劫持为已知密码的演示号）。

### 2. 授权分层
- **staff 专属**：自愈动作（含命令执行）、通知渠道（含 SMTP 凭据）、操作审计、
  清理加速中心全部接口、拨测的创建/启停/手动执行（防借平台做内网探测）、
  接入中心完整令牌（非 staff 掩码显示）。
- **演示访客（只读组）**：所有非 GET 请求 403 + 敏感页前缀拦截
  （`DEMO_BLOCKED_PREFIXES`）。
- **普通登录用户**：面板查询与常规运营操作（大盘/策略/故障单/演练）——
  设计取舍：单管理员自用系统，不设更多中间角色；如需收紧，给相应视图统一
  挂 `_staff_required` 即可（`ops/views.py`、`alerts/views.py` 各有一份现成实现）。

### 3. 输入防御
- 上报 API：数值统一 `math.isfinite` + 范围钳制（防 NaN/Infinity/超 2^62 溢出
  SQLite 报 500）；日志级别白名单；单批条数上限（200/500）与 payload 大小限制
  （超限 413）；主机/自定义指标字段截断。
- 日志检索：LIKE 通配符 `%`/`_`/`\` 转义，防扫描放大。
- 全项目无 raw SQL/字符串拼接 SQL；唯一 `cursor().execute` 为常量 PRAGMA。
- 速率限制：上报端点 120 次/分钟（`monitor/security.rate_limit`，按客户端 IP
  固定窗口）；手动采样 20/分；清理 10/分、磁盘扫描 6/分、内存整理 4/分。

### 4. XSS 防护
- 服务端渲染的动态 JSON（调用链 spans、数据库 by_path）一律
  `json_script` 安全嵌入——防存储型 XSS（攻击者构造恶意路径/SQL 被采集后
  在管理员查看调用链时执行）。
- 前端所有 innerHTML 插值点经 `esc()`（`static/obs/common.js`，覆盖
  `& < > " '`）；ECharts tooltip 统一转义（bar/pie 公共 formatter + 热力图
  自定义 formatter）。
- 模板中 0 处 `|safe`、0 处 `autoescape off`。
### 5. SSRF 防护（出站请求）
- 拨测 / 自愈 HTTP 回调 / 通知 Webhook 统一经 `ops/urlsafe.validate_url`：
  仅 http/https、域名解析后逐 IP 阻断链路本地（含 169.254.169.254 云元数据）、
  保留、组播地址；环回与私网可用环境变量关闭。
- 全部出站请求走**禁止重定向**的 opener（30x 直接视为失败）——防外网跳板
  302 把请求引向内网绕过 IP 黑名单。
- 已知边界：校验与连接之间存在 DNS rebinding TOCTOU 窗口（`ops/urlsafe.py`
  文档注释已声明）；内网部署如需彻底封堵可固定解析 IP 直连。

### 6. 命令执行与清理（高危动作）
- 自愈 `command` 类型：`OBS_HEAL_CMD_ALLOWLIST` 环境变量登记可执行文件绝对
  路径，未配置则**整体禁用**；`shell=False` + 参数列表执行；**拒绝 .bat/.cmd**
  （Windows 下会经 cmd.exe 重新解析参数造成注入）；`cleanup_tmp` 仅允许
  系统临时目录及其子目录（resolve 后校验前缀）。
- 清理加速中心（`cleaner/services.py` / `cleaner/views.py`）：清理目标白名单化
  （系统临时文件 >24h / `__pycache__` / pip 缓存 / 平台过期数据）；遍历与删除
  **剪枝符号链接与 Windows Junction**（`_is_reparse`：islink + isjunction +
  REPARSE_POINT 属性，fail-closed——防挂载点把删除引出白名单根）；文件数/
  扫描时长双上限；清理/VACUUM/内存整理进程级互斥（防重复 VACUUM 持 SQLite
  写锁阻塞在线请求）；全部动作留痕 + 审计。
- 内存整理仅处理当前用户进程（跳过 PID<100、SYSTEM/服务账户、平台自身），
  工作集修剪可逆，页面标注"释放量为估算值"。

### 7. 数据与隐私
- 慢查询落库前对 password/passwd/pwd/secret/token/credential/api_key/
  authorization/auth_token 等关键词的字面量（含 `_hash`/`_key` 类后缀、
  `=`/`:`/`LIKE`/`IN` 形式）做 `'***'` 掩码（`monitor/middleware.py`
  `_sanitize_sql`，普通列名如 `author_id`/`passing_score` 不受影响）——
  APM/调用链展示不泄露凭据。
- SMTP 授权码 Fernet 加密落库（`ops/crypto.py`，SECRET_KEY 派生密钥），
  页面不回显；DEBUG 报错页对令牌/密码所在视图启用
  `sensitive_variables` / `sensitive_post_parameters`。
- 登录页演示凭据三重校验：仅 DEBUG、只取文件第一组、账号必须真实属于
  "演示访客"组。
- 客户端 IP 默认只信 `REMOTE_ADDR`（XFF 可被伪造）；仅在可信反代后开
  `OBS_TRUST_XFORWARDED_FOR=1`。审计、限速、登录锁定、地域统计共用该开关。
- CSV 导出防公式注入：`= - + @` 开头单元格加前缀。
- 机密文件（`ingest_token.txt` / `.secret_key` / `demo_credentials.txt` /
  `ops_credentials.txt`）已列入 `.gitignore`。

### 8. 审计与可观测
- 全部高危动作写 `AuditLog`（自愈执行、清理、内存整理、策略变更、大盘编辑、
  登录成败）；审计写入失败记录日志（安全日志不允许静默失败）。
- 审计日志保留 90 天（后台自动清理）；采集数据按保留期分批清理（防长事务锁库）。

## 四、三轮红队审查记录

| 轮次 | 重点 | 主要发现（均已修复） | 验证 |
|---|---|---|---|
| 第一轮（全量审计） | 代码 + 模板 + ops | 存储型 XSS（模板 safe 过滤器渲染采集数据）；自愈 command 无白名单 RCE；SSRF 重定向绕过；DEBUG/ALLOWED_HOSTS 硬编码；Prometheus 格式非法；告警恢复通知丢失；日志级别 KeyError 500；上报数值溢出 500 等 ~90 项 | 页面遍历 + 安全行为用例 |
| 第二轮（红队视角） | 防护绕过与回归 | **运行中服务未重载新代码**（--noreload，最大实际风险）；令牌可读数据 API（收窄作用域）；登录无限流；上报零限速；拨测缺 staff 门禁；`.bat` 白名单绕过；`probing.py` 缺 `import socket` 回归 | 黑盒渗透套件（匿名/令牌/限速/爆破） |
| 第三轮（cleaner 专项） | 新模块 + 回归 | **Windows Junction 逃逸**清理白名单（islink 不识别挂载点）；遍历死循环 DoS；登录锁定可被用于锁死受害者（DoS）；SQL 脱敏误伤 `author_id` 类列名 | 真实 Junction 创建实测、正则用例、锁定流程用例 |
| Mimosa 深度扫描 ×3 | 独立工具复核 | 每轮 0 高危 0 中危；66 条低危均为演示数据生成用 `random`（非安全用途误报） | 扫描封印报告 |

## 五、验证方法（可复现）

```bash
# 系统检查
python manage.py check

# 自动化安全回归（首选这一条，比手敲 curl 更可复现、也不会漏测分支）
python manage.py test ops.tests cleaner.tests monitor.tests
#   ops/tests.py      -> SSRF 闸门（协议/凭据/元数据地址/私网开关/DNS 结果复核/禁重定向）、
#                        自愈命令白名单（未登记即禁用、拒 .bat、参数不经 shell、30s 超时）、
#                        cleanup_tmp 作用域（临时目录之外一律拒绝）、冷却期原子抢占、
#                        SMTP 授权码密文落库与换密钥的优雅失败
#   cleaner/tests.py  -> Junction/符号链接剪枝（含"删除越界"逃逸用例）、
#                        遍历限时限量、只删超龄期文件、未知清理项拒绝、VACUUM 的 autocommit 约束
#   monitor/tests.py  -> 令牌作用域与越权、限速 429、SQL 脱敏、traceparent 非法输入、
#                        采集缓冲不静默丢数据、任务租约独占与接管

# 页面回归（36 个页面应全部 200）
# 黑盒渗透要点（curl）：
#   匿名：所有页面 302 / 数据 API 401 / 上报 401 / 路径穿越变体 401-404
#   令牌：可写 /api/ingest/、/logs/api/ingest/、/rum/beacon/、可抓 /metrics；
#         读类 API 与页面一律 401/302
#   限速：连发 130 次上报 -> 120 次 200 + 10 次 429
#   爆破：错 10 次 -> 错密码 429、正确密码放行并清零
#   演示组：写操作 403、敏感页 403
```

## 六、生产部署加固清单

1. `DJANGO_DEBUG=0`（关闭调试页——局部变量虽已对敏感视图屏蔽，仍应关闭）
2. `DJANGO_ALLOWED_HOSTS=你的域名` + `DJANGO_ALLOW_ALL_HOSTS=0`
3. HTTPS 反代后：`DJANGO_SECURE_COOKIES=1`、`OBS_TRUST_XFORWARDED_FOR=1`
   （并统一 XFF 取跳方向）
4. `OBS_INGEST_TOKEN` 换成高强度随机值并定期轮换（轮换：删除
   `ingest_token.txt` 重启，同时更新各 Agent）
5. 多进程/多副本部署：后台任务由租约选主（`monitor_tasklease` 表），副本数不限、
   无需再手工指定 `OBS_WORKERS_ENABLED`；限速/登录锁定仍是进程内 cache，
   要跨进程共享就把 `CACHES` 换成 Redis
6. 数据库换 PostgreSQL；删除 `demo_credentials.txt` / `ops_credentials.txt`
7. 自愈需要命令时才配置 `OBS_HEAL_CMD_ALLOWLIST`（只登记 .exe/.py，永不登记 .bat）
8. 反代层对 `/accounts/login/` 追加更严格的限速或验证码

## 七、已知权衡与接受风险

| 项 | 权衡 | 触发条件才需处理 |
|---|---|---|
| 登录锁定计数/限速为进程内 cache | 多进程部署各自计数 | 上 gunicorn 多进程时换 Redis cache |
| DNS rebinding TOCTOU 窗口 | 出站校验后连接前域名可再解析 | 严格内网隔离场景固定 IP 直连 |
| 普通账号可操作面板类写接口 | 单管理员系统的角色简化 | 创建普通账号后按需挂 staff 门禁 |
| `?token=` 查询串兼容 | 可能进入访问日志/Referer | 全部 Agent 升级到请求头后可移除 |
| DEBUG 默认开 | 便于开箱演示 | 对外暴露前设 DJANGO_DEBUG=0 |
