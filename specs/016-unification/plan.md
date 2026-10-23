# 两版统一实施与验收

开始日期：2026-10-23。用户在完成 GitHub 源码研究后要求“开始执行”。承接 [统一方案](../research/06-GitHub学习与两版统一.md)，仍在六功能范围内；目标是一个公开网站、一份活动业务库、一套业务任务和通知出口。旧代码、数据库、配置保留私有归档，不删除课程原件。

## 并行任务与责任

| 任务 | 输入与输出 | 依赖与负责文件 | 完成条件 |
| --- | --- | --- | --- |
| 数据与资讯 | 原版提供器、已固定 GitHub 研究 → 带来源和时间的行情、资讯、公告、日历与增量快照 | 新增 backend/app/research_*.py、backend/tests/test_research_*.py；不改 main/store | 缺失/过期/重复/数字变化有测试；实际源检查；供网页和 AI/简报共用 |
| 网页整合 | 本文接口 → 自选分组、持仓、顺序、撤销、单股入口、综合行情资讯、历史档案 | frontend/src 下文件；依赖后端接口，独立实现 | 类型检查，桌面/窄屏真实流程，保留旧事件/报价链接 |
| 解释与简报 | 冻结行情和研究快照 → 带可核对引用的解释、丰富的早盘简报 | explanation_*、quote_evidence.py、briefing_* 及对应新增测试；不改 main/store/frontend | 引用/过期/数字改动/模型失败/预算不足验证，旧记录仍可读 |
| 主线整合 | 两库结构、以上接口 → 管理字段、更多异动、幂等迁移、唯一任务交接、发布 | store.py、main.py、quotes/anomaly、迁移与部署工具、集成验收和文档 | 真实库副本迁移与旧代码兼容、浏览器联调、公网一致、旧任务停用且归档可核验 |

## 公共接口约定

保留已有路径和响应字段，新增字段可选并兼容历史。主代理维护 main.py、数据库公共结构和发布；子任务不部署、不发送外部消息。网络调用不在 SQLite 写事务内，测试用临时库且关闭外部发送。

- 自选 `WatchItem` 增加 `group_id: string|null`、`is_holding: boolean`、`display_order: integer`、`metadata_version: integer`。`GET /api/watchlist/groups` 返回 `{items:[{id,name,created_at}]}`；`POST` body `{name}` 新建；`PATCH /api/watchlist/groups/{id}` body `{name}` 重命名；`DELETE` 将组内股票移到未分组，不删股票。
- `PATCH /api/watchlist/{market}/{symbol}` body `{watchlist_created_at,expected_version,group_id,is_holding}`；成功 `{item}`。`POST /api/watchlist/reorder` body `{items:[{market,symbol,watchlist_created_at,metadata_version}]}` 包含完整列表，成功 `{items,total}`。删除响应保留 `removed`，另含 `undo_token` 和 `undo_expires_at`；`POST /api/watchlist/undo/{token}` 恢复身份与管理字段，重新开启一代自选，旧事件和解释保持原代次且不会补发。
- 研究服务 `ResearchService(store, provider=None, clock=None)`：`initialize()`、`state(market=None,symbol=None)`、`refresh(market=None,symbol=None)`、`freeze_context(market=None,symbol=None,cutoff=None)`。市场与股票同时为空代表综合视图。`GET /api/research` 和 `GET /api/research/{market}/{symbol}` 只读缓存；各自 `POST .../refresh` 有持久/并发冷却及有界请求；最多同时一个刷新任务，超时/失败不隐藏旧数据或冒充成功。
- 研究响应 `{scope,market,symbol,snapshot_id,created_at,refresh_state,last_attempt_at,retry_after_seconds,error_message,sections}`。`sections` 为数组，每项 `{key,title,status,source,source_url,as_of,received_at,error_message,items}`；key 为 `indices/global/sectors/news/announcements/calendar`，status 为 `available/partial/unavailable/stale`。行情条目 `{id,name,code,price,change_pct,as_of,source,source_url,received_at}`；资讯/公告/日历条目 `{id,title,summary,url,published_at,received_at,source,change_kind}`，`change_kind` 为 `new/updated/unchanged`。未知字段为 null，不填零；条目可含更多来源明确字段。`freeze_context` 返回同结构不可变快照，按 cutoff 过滤未来发布项并明确过去数据缺失；不得在 GET/AI 校验事务里隐式联网。
- 解释与简报保留现有字段，可增加 `research_context`、`research_evidence`、`research_used_count`、`trace`；新解释响应仍走现有持久记录、请求去重和预算。资讯引用须对应本次输入的固定 ID、URL、发布时间和抓取时间，不把有新闻当作已证明因果。
- `GET /api/legacy/history?kind=&symbol=&limit=&cursor=` 返回 `{items:[{id,kind,source_id,occurred_at,symbol,status,content}],next_cursor,total}`；只读迁移档案。`GET /api/legacy/migration` 返回无秘密的迁移概要和冲突/待确认信息。旧未发送队列只归档，不进入活动队列。
- 额外异动规则接口由主代理追加，保留当前涨跌阈值和所有历史 ID；新规则默认关闭至资料资格与逐项验收通过，不伪造上市日期或证券涨跌停资格。

## 必须取得的验收证据

1. 管理字段与跨设备保存、顺序并发冲突、删除撤销及旧历史定位；现有清单、规则、通知和预算不丢失。
2. 原版指数/板块/全球/资讯/公告/日历覆盖逐项映射，实际提供器有成功或可解释失败；来源、数据时点、抓取时点和陈旧状态分开。
3. 资讯稳定键和内容指纹：重复内容、新条目、数字或否定变化、过期、未来时间、恶意文本；AI 只用冻结证据，失败与预算限制保留真实状态；简报显示资料截止、缺项、投递状态及新增信息。
4. 原版多类规则逐条映射并进入统一持久事件系统；同日去重、重启保留、真实与演练隔离、数据缺失不触发。自然触线与本人实收仍按事实分别标记。
5. 两个真实数据库的一致性私有副本：字段、关联、冲突、幂等重复导入、预算承接、旧代码兼容；旧 success 不升级本人确认，旧未投递不补发。
6. 新版在 /lab/ 联调后切根入口，旧链接兼容、旧 API 写操作明确终止；仅一套业务进程/定时发送，已有备份/验收任务保持；旧代码/数据/配置私有归档可核验，回退不覆盖新增数据。
7. 白名单纯代码包、服务器完整验证、发布标识/文件哈希、公网免账号免密码和实际流程。将已完成与待自然观察项目分别记录，不用健康检查代替功能验证。

## 来源与复用记录

第三方仅借鉴已核查的方法，新增代码独立实现；若复用原版独立提供器，在文件与验收中记录来源及实际差异。保持现有 SQLite、FastAPI、React 和单 worker，不新增收费资源。
