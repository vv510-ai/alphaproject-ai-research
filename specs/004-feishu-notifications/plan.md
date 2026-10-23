# 第十一课：飞书通知通道

状态：实现、检查与真实发送已完成，本人已反馈收到，网页核验码尚待提交，见[本课验收记录](acceptance.md)。2026-10-18 用户要求接入只有自己一人的群，并明确“原来的项目文件就已经配置好了，直接搬过去”。本课只复用原项目 4 个飞书配置字段，原文件保持原样；不复制其他配置或成品代码，不发布学习工程到生产网站。

## 要做出的结果

页面查看接收群 → 核对群仍为私有群、1 位成员和 1 个机器人 → 开启通知 → 主动发一条明确标注的测试消息 → 记录飞书返回结果 → 用户在飞书看到核验码并在页面确认。关闭时取消尚未提交的测试；已在发送中的请求可能仍送达。

本课复用原项目的应用机器人，使用已有 HTTPX 调用飞书官方接口，不安装另一个 SDK。先前只读核验显示群名“量化推送”、机器人“vv小助手”，成员身份还会在配置导入时与群主核对并绑定。每次发测试前重新检查同一群与成员；成员变化则停止。

## 实现顺序与理由

1. 配置导入脚本只读取 `LARK_APP_ID / LARK_APP_SECRET / LARK_RECEIVE_ID / LARK_RECEIVE_ID_TYPE`，认证和群核对通过后用 Windows 当前用户的 DPAPI 加密保存到 `.runtime/feishu-config.dpapi`。密钥不传给浏览器，不打印，不进入版本库。换电脑或用户须重新导入，不声称加密文件可跨机恢复。
2. 后端新增通知设置、发送记录两张表；已有自选、报价和日线不改写。网络请求在数据库事务外执行，先记录再发送。
3. 每次点击使用唯一请求号。重复提交返回同一记录；结果未知不自动重发，重启也不重发。一次只处理一条测试，30 秒间隔防连点。
4. 区分未提交、发送中、平台已接收、明确失败、结果未知、已取消。平台已接收与用户确认收到分别展示。核验码仅出现在飞书正文，网页接口不返回正文或核验码。
5. 真页面执行开关和发送，用户参与收件核对；模拟异常检查与真实送达证据分开。

## 依据与边界

- [飞书应用机器人说明](https://open.feishu.cn/document/client-docs/bot-v3/bot-overview)：本次已有应用机器人，具备已实测的群信息读取能力。
- [发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)、[文本消息格式](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json)：固定群发送 text，content 为 JSON 字符串；使用唯一 uuid，但本工程不假设服务端去重期限。
- [应用访问凭证](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal)：凭证只在后端内存短期缓存。
- [Windows CryptProtectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)：使用当前用户范围，不用 LOCAL_MACHINE。
- 当前是局域网个人共享应用，能访问页面的人可操作测试通知；保留同源修改检查、固定接收群和发送间隔。不开新平台、不加收费资源、不建定时任务。
- 尚不做异动自动提醒、AI 解释或早盘简报；后续都复用本课验证过的通道。没有用户收件反馈时，不写“真实收件已验收”。
