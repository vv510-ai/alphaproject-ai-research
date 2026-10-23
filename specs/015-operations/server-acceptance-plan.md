# 服务器自动验收迁移（2026-10-22）

用户要求：把开盘后的自动验收迁到现有服务器，不依赖电脑或 Codex；持续采集和茅台 3% 提醒有实际结果才通知。沿用已授权的学习版与现有私人飞书群。

| 工作 | 输入与输出 | 负责文件 | 依赖与完成条件 |
| --- | --- | --- | --- |
| 运行与通知 | 复用只读验收脚本，持久化独立证据、去重和飞书回执 | scripts/server_acceptance.py；backend/tests/test_server_acceptance.py | 使用现有 FeishuClient；并发、崩溃恢复、休市安静、目标变化、通知不确定均有验证 |
| 服务器调度安装 | 固定版本的低权限 oneshot 服务、工作日定时器及可回退安装 | deployment/cloud/acceptance.py、acceptance.service、acceptance.timer；backend/tests/test_acceptance_install.py | 实际服务执行成功后启用；验收与通知状态不覆盖生产数据库 |
| 独立审查 | 检查两个实现的交接、边界和实际部署证据 | evidence/server-acceptance-20261022/review.md；backend/tests/test_server_acceptance_review.py | 不修改他人文件、不自行部署，发现问题先反馈 |
| 主集成与发布 | 凭据访问、代码包清单、发布命令、文档和公网复验 | 公共接口、artifact.py、deploy.py、remote_release.py、feishu_config.py、runtime_config.py、现有文档 | 真实服务器任务与飞书结果回执可核验后删除本机自动回查 3 |

接口约定：`scripts/server_acceptance.py --state-dir /var/lib/alphaproject-lab-acceptance`。服务名 `alphaproject-lab-acceptance.service`，同名 timer。使用现有 `alphaproject-lab` 用户，仅加载 `feishu.json`；不加载模型凭据，不初始化应用 NotificationService，不修改生产数据库。脚本通过 HTTPS GET 读取现有应用；只向已核对的原有私人群发送确有依据的验收结果。配置/目标改变不擅自恢复。

服务固定到本次发布目录和虚拟环境。定时器为北京时间工作日 09:35、15:35，Persistent=true，AccuracySec=1s。独立状态目录保存 state.json、last-run.json、runs/ 和通知账本。进程锁防重入，发送前持久化意图，崩溃或不确定投递不自动重发。首次安装执行真实任务；定时器启用并复核后写 installation.json，由同一个服务发送一次真实迁移完成回执。此回执不得冒充盘中验收通过。

`installation.json` 合约：schema_version=1、release_id、installed_at（带时区）、timer={active:true,enabled:true,schedule:"Mon..Fri 09:35,15:35 Asia/Shanghai"}、receipt_key="server-acceptance-installed-v1"。安装脚本把它安全写入独立状态目录；运行器核对其版本、release_id 与正在执行的代码版本、合法时间和完整字段后生成一次迁移结果候选。`last-run.json` 至少包含 status、started_at、completed_at、probe（原验收报告）、deliveries（candidate_keys/status/provider_message_id）、terminal（空或 completed/suspended）。安装器先验最后一次运行成功且时间为本次启动之后，再启用定时器、写安装记录、再次启动真实服务，确认本次迁移回执 accepted；不确定回执不重发、不宣称迁移全部验收完成。

两个自然目标均有效通过且结果通知获得平台回执后写 completed.json；规则改变通知处理后写 suspended.json。服务用 ConditionPathExists 跳过终止状态；它们只终止本次验收观察，不影响应用持续采集和原有异动通知。保留平台接受与本人实际收件确认的区别。服务器整机离线时无法依靠本机任务发出即时故障通知。

2026-10-22 核对的官方依据：沿用服务器 systemd 255 的 [timer 语义](https://www.freedesktop.org/software/systemd/man/255/systemd.timer.html)（同一活动服务不会重入，Persistent 可在恢复后补起一次），[凭据与触发环境](https://www.freedesktop.org/software/systemd/man/255/systemd.exec.html)（LoadCredential 提供私有只读文件，TRIGGER_* 仅为尽力提供的触发证据），以及现有 [飞书发送消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/create)。本轮复用已运行的 FeishuClient，不依赖供应商短期去重代替本地持久账本，不新增平台或收费资源。
