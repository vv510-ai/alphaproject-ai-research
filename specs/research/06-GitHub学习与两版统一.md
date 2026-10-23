# GitHub 学习与两版统一方案

核验日期：2026-10-23（北京时间）。对应“两版对比、学习相关 GitHub 项目、吸收优点并统一到阿里云”的讨论。

**建议以 AlphaProject-Lab 的事件、历史、通知和预算机制为基础，补齐原版的信息覆盖，再选择性吸收 GitHub 项目的界面与证据组织方法。完成替代范围验收后统一入口、停旧任务，旧代码和数据库保留归档。**

这轮完成源码研究与方案比较，保存了固定提交、许可证、关键文件、维护记录以及两版公网页面和版本。第三方应用尚未在本机或阿里云安装运行；研究建议不等于功能已接入。六功能继续作为边界：自选、行情、异动、AI 解释、通知、早盘简报。

## 1. 先学哪些项目

| 项目 | 最值得学的内容 | 对本项目的取舍 |
| --- | --- | --- |
| [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) | 行情质量字段、多源失败处理、报告、新闻实际使用记录、告警与投递记录分层 | **工程流程优先参考。**适配少量结构或独立模块；整仓还包含海外 SDK、搜索、选股等依赖。 |
| [vibe-astock](https://github.com/simonlin1212/vibe-astock) | 报价资格、交易日历失败处理、任务状态、当日补跑；自选与复盘工作台 | **盯盘与调度优先参考。**浏览器界面可云端运行，但远程访问口令和 Node 编排需要适配。 |
| [OpenStock](https://github.com/Open-Dev-Society/OpenStock) | 自选列表进入详情，在同一行查看价格、涨跌并创建提醒 | **界面优先参考。**当前自身报价/提醒可调用的免费行情主要覆盖美股与加密资产，自选依赖登录和 MongoDB。 |
| [go-stock](https://github.com/ArvinLovegood/go-stock) | A 股资讯与分析的组织、工具调用进度、耗时和模型用量记录 | **交互与过程透明度参考。**Go/Wails 桌面模式需要重做 Web 适配。 |
| [stockwatch](https://github.com/v0id-byte/stockwatch) | SQLite 提醒历史、网页提醒、成功后标记通知、按缺失股票补查备用源 | **轻量实现的阅读样本。**需要补齐行情时间、未知值和发送中断的处理。 |
| [a-share-research-monitor](https://github.com/nexaforgelab/a-share-research-monitor) | 保存上次快照，识别新公告/新闻或内容更新，生成变化报告 | **资讯增量方法参考。**目前只有一次提交，推送和调度不能直接承担我们的线上要求。 |
| [TradingAgents-CN](https://github.com/hsliuping/TradingAgents-CN) | 数据、分析、报告分层；失败降级与质量检查的边界 | **复杂分析流程参考。**部分应用目录为专有源码可见，依赖明显扩大；Agent 数量不等于准确率。 |
| [magpie](https://github.com/SymbolStar/magpie) | 小型规则引擎、HTTP 接口、SQLite 提醒记录 | **接口参考。**是后台 daemon，未见人用网页及内置 AI 解释；内存调度和失败仍冷却的行为不符合现有要求。 |

AKShare、Lightweight Charts 等现有组件继续沿用已验证接入，不因发现新仓库重新选整套技术栈。原先 18 项初筛保留在[第四课记录](03-开源项目.md)，其中统计日期仍为 10 月 16 日。

## 2. 两版现在各有什么价值

10 月 23 日 04:47 的公网读取确认：原入口为 alphaproject-20261015T211612763703Z，学习版为 v0.10.4 / lab-20261022T195932667353Z。这次读取确认页面和发布标识，不代替自然交易验收。[公网记录](../../evidence/unification-research-20261023/public-comparison.json)

| 主线 | 原版值得保留 | 学习版值得保留 | 统一后的建议 |
| --- | --- | --- | --- |
| 自选 | 分组、持仓标记、顺序、删除撤销 | 市场/代码校验、加入代次、跨设备保存 | 补回需要的管理字段，统一一份清单。 |
| 行情 | 指数、板块、综合看板、较宽数据覆盖 | 来源时间与接收时间分开、冻结报价依据、陈旧提示 | 每块数据都说明来源、日期和失败情况。 |
| 异动 | 多类规则及其界面 | 持久规则/事件/依据，同日去重、真实与演练隔离 | 原版规则逐条适配，进入同一事件系统。 |
| AI | 新闻、公告、板块上下文和综合解释路线 | 固定证据、输出核验、预算预留、历史与人工核对 | 先保存资讯，再区分事实、可能原因、缺失证据。 |
| 飞书 | 队列、限速、稳定逻辑消息 UUID | 持久投递意图、平台接受与本人确认分开、未知不自动重发 | 一个发送出口，历史不因迁移重发。 |
| 简报 | 全球市场、板块、新闻、日历 | 自选事实摘要、报告关联、自然任务领取记录 | 保留一份早盘简报，注明资料截止、缺项与投递状态。 |

原版并非所有开关都能算作可靠能力。实际行情适配器向 QuoteSnapshot 传入不存在的 volume 参数，隔离提取原定义后已复现 TypeError；三个来源文件与原版发布包一致。实验没有启动应用、访问数据库或调用行情，**证明的是构造接线错误，尚未取得线上扫描失败日志**。[复现证据](../../evidence/unification-research-20261023/legacy-adapter-reproduction.json)

学习版也有缺口：AI 未接入新闻/板块原因证据，简报仍是自选日线事实摘要。最近留存验收中，自然盘中、自然触线、本人实收和手机实机仍待验；本轮未重判这些状态。详细比较见[独立架构复核](../../evidence/unification-research-20261023/architecture-review.md)。

## 3. 从源码真正学到的做法

### 统一行情含义，再增加来源

DSA 的报价对象分别保存来源、抓取时间、提供方时间、过期程度、备用来源和缺失字段，能够表达“刚收到旧行情”“换源后缺字段”。学习版已有时间和资格判定，可补来源健康与失败诊断，不必重写现有数据层。[统一报价结构](https://github.com/ZhuLinsen/daily_stock_analysis/blob/ec66bf46caf6993f673bd4be59c883754667b686/data_provider/realtime_types.py)

StockWatch 只给缺失股票补查备用源，减少全列表重复取数；但备用结果存在时间为空、未知字段填零的情况。可学补查策略，不能直接让这类结果通过当前异动资格检查。[行情入口](https://github.com/v0id-byte/stockwatch/blob/114738289eb7c60e7569af73fd6c9fbc7fc94051/data/market.py)

### 任务记录要能解释为什么没运行

vibe-astock 在交易日历缺失或日期不匹配时不默认开市；报价缺少参考交易日时标为未核验。定时任务保存状态、当天完成记录、失败次数与下次重试时间，值得学习其状态表达和有限补跑。其日线正成交量辅助校验不等于本项目的连续盘中更新证明；继续保留现有更细的采样条件。[报价资格](https://github.com/simonlin1212/vibe-astock/blob/7988c991eaac0adee8b49bf3ee204f6a21e437aa/orchestrator/src/finance/quote_freshness.ts)、[日历检查](https://github.com/simonlin1212/vibe-astock/blob/7988c991eaac0adee8b49bf3ee204f6a21e437aa/orchestrator/src/finance/schedule.ts#L65-L84)

它的任务完成也不等于飞书成功：所有外部渠道失败可以作为说明文字返回，runJob 仍记录 ok。可以学任务进度和恢复规则，不能替换现有平台回执与本人确认的区分。[任务与投递](https://github.com/simonlin1212/vibe-astock/blob/7988c991eaac0adee8b49bf3ee204f6a21e437aa/orchestrator/src/notify.ts#L541-L615)

### 报告突出新增信息，保留当时证据

a-share-research-monitor 使用稳定条目键和内容指纹，跳过相同快照，将首次出现与内容更新分开；报告保留来源链接和待核实项。适合让简报突出新增公告、内容变化和待核实事项。其“语义去重”采用字符重合、关键词和字符串相似度，不能据此断言理解了所有语义；数字变化和否定句需要验证。[变更检测](https://github.com/nexaforgelab/a-share-research-monitor/blob/87940e4c87d2bde2af56813decb701bdb26d39a1/src/diff/detector.py)、[相似度](https://github.com/nexaforgelab/a-share-research-monitor/blob/87940e4c87d2bde2af56813decb701bdb26d39a1/src/diff/semantic.py)、[报告](https://github.com/nexaforgelab/a-share-research-monitor/blob/87940e4c87d2bde2af56813decb701bdb26d39a1/src/report/renderer.py)

DSA 记录真正交给 Agent 的新闻条数，区分搜索不可用、没有消费新闻和使用了新闻。可学“不把事后补搜当作当时证据”；计数不代替引用核验。统一版仍需保存新闻 ID、URL、发布时间、抓取时间和摘要，让解释引用已冻结的记录。[新闻消费记录](https://github.com/ZhuLinsen/daily_stock_analysis/blob/ec66bf46caf6993f673bd4be59c883754667b686/src/agent/news_evidence.py)

### 展示分析过程，事实检查仍须逐项对应

go-stock 记录工具名称、状态、耗时和模型返回的 token 用量，有助于看懂分析卡在哪里。它的金融事实提醒只是检查“回答含金融事实，但本轮没有成功工具调用”；任意成功调用即可消除提示，提示也不修改已流出的正文。可参考进度展示，不能据此宣称每个数字都已核对。[调用记录](https://github.com/ArvinLovegood/go-stock/blob/f49a730333c1d3108d7f2fa8b49c6b77a20d256a/backend/agent/agent_trace.go)、[提醒逻辑](https://github.com/ArvinLovegood/go-stock/blob/f49a730333c1d3108d7f2fa8b49c6b77a20d256a/backend/agent/agent_fact_check.go)

TradingAgents-CN 的质量门禁也明确不检查引用。统一版应保留现有数字与证据约束，再补资讯引用核验，不用报告长度、结论关键词或多个 Agent 一致意见代替事实检查。[门禁范围](https://github.com/hsliuping/TradingAgents-CN/blob/51060a7682cf51810b4bc3a704829438d18cd8dc/core/agents/quality_gate.py)

### 区分触发、发送尝试与实际回执

DSA 数据库告警区分触发记录、通知尝试与冷却，渠道成功才更新相应冷却。另一个 notification_noise 是纯内存抑制器；飞书单次重试复用 UUID，也不等于跨进程持久幂等。[告警 worker](https://github.com/ZhuLinsen/daily_stock_analysis/blob/ec66bf46caf6993f673bd4be59c883754667b686/src/services/alert_worker.py)、[内存抑制器](https://github.com/ZhuLinsen/daily_stock_analysis/blob/ec66bf46caf6993f673bd4be59c883754667b686/src/notification_noise.py)、[飞书发送器](https://github.com/ZhuLinsen/daily_stock_analysis/blob/ec66bf46caf6993f673bd4be59c883754667b686/src/notification_sender/feishu_sender.py)

OpenStock 发邮件前原子领取提醒，防止任务同时处理；若领取后、发送前进程退出，这段代码未见自动恢复领取的租约机制，因此不能宣称恰好一次投递。可学并发领取，学习版已有的发送中断与 unknown 状态继续保留。[提醒执行](https://github.com/Open-Dev-Society/OpenStock/blob/855ca6af33ae0b86f8835ecda6c31707f4ec3891/lib/inngest/functions.ts#L199-L319)

a-share-research-monitor 飞书推送只检查 HTTP 成功，没有检查 JSON 业务错误码；主流程在推送之前提交快照，失败不会自动恢复为待发送记录。“报告写成”与“消息收到”需要独立保存，不能直接替换现有通知链。[发送](https://github.com/nexaforgelab/a-share-research-monitor/blob/87940e4c87d2bde2af56813decb701bdb26d39a1/src/report/pushers.py)、[主流程](https://github.com/nexaforgelab/a-share-research-monitor/blob/87940e4c87d2bde2af56813decb701bdb26d39a1/src/main.py#L74-L109)

### 同一只股票的操作放在一起

OpenStock 自选行将价格、涨跌、详情和创建提醒放在一起，缺报价时显示占位。统一版可参考这一组织：从股票直接看依据、设阈值、看历史事件与解释，减少在多个页面寻找入口。保留本项目公开免登录方式。[自选表格](https://github.com/Open-Dev-Society/OpenStock/blob/855ca6af33ae0b86f8835ecda6c31707f4ec3891/components/watchlist/WatchlistTable.tsx)

## 4. 版本与复用边界

下表日期是所读默认分支提交的 UTC 日期；Release 是单独读取的发布信息，包版本取自 package.json，二者不混用，也不代表我们运行过发布包。

| 项目 | 固定提交短码 / 提交日期 | Release 核查 | LICENSE 文件与本轮使用 |
| --- | --- | --- | --- |
| daily_stock_analysis | ec66bf46caf6 / 2026-10-21 | v3.33.0，2026-10-15 | 根 MIT，第三方子目录另有声明；少量独立模块可继续评估复制，保留来源。 |
| vibe-astock | 7988c991eaac / 2026-10-18 | 包版本 2.1.2，本轮未核验 Release 列表 | 根 Apache-2.0，回测另有上游许可文件；本轮学报价资格与调度。 |
| OpenStock | 855ca6af33ae / 2026-10-20 | v2026.10.18 | AGPL-3.0；学习界面行为，代码取用按集成方式另核条件。 |
| go-stock | f49a730333c1 / 2026-10-22，默认 dev | v2026.10.22.1-release | GPL-3.0；学习产品与调用记录，未移植桌面代码。 |
| stockwatch | 114738289eb7 / 2026-09-01 | releases 返回空 | 文件自述 Apache-2.0，API 未标准识别；只学方法。 |
| a-share-research-monitor | 87940e4c87d2 / 2026-05-23 | latest-release 返回 404，不据此排除草稿/预发布 | 文件 Apache-2.0；只学结构与增量方法。 |
| TradingAgents-CN | 51060a7682cf / 2026-09-22，默认 v3.0 | releases 返回空 | 分目录 Apache-2.0 / 专有源码可见；app、core、frontend 不纳入复制清单。 |
| magpie | 177d8bb089c8 / 2026-05-14 | 包版本 0.0.5，本轮未核验 Release 列表 | MIT；仅作轻量规则接口参考。 |

许可证保存在[首组三仓报告](../../evidence/unification-research-20261023/upstream-review.md)和[补充快照目录](../../evidence/unification-research-20261023/root-snapshots/)。这里记录文件范围，不作笼统法律判断；代码许可也不代替数据使用条件。

维护反证影响取舍：StockWatch 近期绿色任务是 CodeQL，幂等调度改造仍在开放 PR；DSA 部分定时任务成功不等于所有业务路径通过；[go-stock #190](https://github.com/ArvinLovegood/go-stock/issues/190) 报告 AI 分析后卡顿。这些属于需验证的线索，不能仅据 Issue 存在就认定本次快照仍能复现。详细记录见证据目录。

## 5. 建议怎样统一

1. **统一使用路径。**以学习版为基础补回需要的自选管理、指数/板块信息；参考单股内完成看行情、设提醒、看依据的组织。内部使用同一报价、事件和通知模型。验收添加、刷新、重启、历史定位和缺数据显示。
2. **补齐解释与简报。**先评估原版已有新闻/公告/板块提供器，保存带来源与时间的证据；借鉴增量快照突出新增信息。验证重复新闻、过期新闻、标题数字变化、模型失败与预算不足。没有来源时仍可展示价格事实，不补造涨跌原因。
3. **演练数据与任务交接。**用两库一致性副本做幂等导入，保留原 ID、来源、分组和历史，核对冲突、条数与关联。旧成功日志不升级为本人确认，旧未投递队列不自动补发。只有一套任务拥有生产发送权。
4. **统一入口并停旧服务。**新版本在现有 /lab/ 完成替代范围验收后，让根入口指向该版本，兼容旧事件与报价链接；停止旧应用及其业务任务，保留旧代码、数据库和配置私有归档。回退保存切换后新增数据，不拿旧库覆盖新库；旧 API 不盲目重定向写请求。

统一后保留一个主业务库、一个消息出口和一套业务任务，现有独立备份与只读验收继续。服务器自然验收仍按既有计划进行。本轮未变更服务、迁移数据、发送消息或切换入口。

## 6. 研究范围与证据

这是围绕六功能的定向研究，不是全网排名。本轮选读 8 项目的关键源码，其中 vibe-astock 与 magpie 是补充初筛；vibe-astock 因报价资格与调度设计升为重点学习对象，但两项的检查范围仍较窄。检索覆盖 A 股/自选/飞书、watchlist/alert、新闻增量与数据源，沿旧候选、源码和近期 Issue/PR 补查。未完成第三方整仓运行、完整安全审计、数据源可用率或模型效果比较，不宣称生态搜索饱和。

- [两版独立复核与迁移验证建议](../../evidence/unification-research-20261023/architecture-review.md)
- [DSA、StockWatch、TradingAgents-CN 源码与反例](../../evidence/unification-research-20261023/upstream-review.md)
- [原版接线隔离复现](../../evidence/unification-research-20261023/legacy-adapter-reproduction.json)
- [公网页面与发布标识](../../evidence/unification-research-20261023/public-comparison.json)
- [补充源码索引](../../evidence/unification-research-20261023/root-snapshots/file-index.json)

