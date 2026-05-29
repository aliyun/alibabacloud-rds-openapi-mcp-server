# 钉钉/飞书/企业微信/QQ Bot 接入 RDS AI 助手

本目录提供 RDS AI 助手机器人接入示例，支持通过长连接模式接入钉钉、飞书、企业微信 WeCom AI Bot 和 QQ Bot。配置完成后，用户可以在 IM 单聊或群聊里直接向机器人提问，机器人会调用 RDS AI 助手返回结果。

> 仅 RDS AI 助手专业版支持机器人接入。配置前请先开通 RDS AI 助手专业版，并准备好阿里云 AccessKey。

## 快速开始

```bash
cd rds-copilot-bot-gateway
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

在 `rds-copilot-bot-gateway/.env` 写入阿里云 AK/SK 和要启用的平台配置。`main.py` 启动时会自动加载当前运行目录的 `.env` 文件。

```bash
python main.py
```

同时启动多个平台：

```bash
RDS_BOT_BRIDGES=dingtalk,feishu python main.py
RDS_BOT_BRIDGES=all python main.py
```

## 通用环境变量

| 变量名 | 必选 | 说明 |
|---|---|---|
| `ACCESS_KEY_ID` | 是 | 阿里云 AccessKey ID，可在 [RAM 控制台](https://help.aliyun.com/zh/ram/user-guide/create-an-accesskey-pair) 创建，建议使用 RAM 用户。 |
| `ACCESS_SECRET` | 是 | 阿里云 AccessKey Secret，创建后仅显示一次。 |
| `RDS_BOT_BRIDGES` | 否 | 启动的平台，默认 `dingtalk`；支持 `dingtalk`、`feishu`、`wecom`、`qqbot`，多个用逗号分隔，`all` 表示全部启动。 |
| `RDS_COPILOT_LOG_FILE` | 否 | 日志文件路径，默认当前运行目录下的 `rds-copilot.log`。 |
| `RDS_COPILOT_CONVERSATION_STORE_FILE` | 否 | 会话状态 JSON 文件路径，默认当前运行目录下的 `copilot_conversations.json`。 |
| `RDS_BOT_STILL_WORKING_INTERVAL_SECONDS` | 否 | 长任务运行中提示间隔，默认 `180` 秒；设为 `0` 可关闭。 |
| `RDS_COPILOT_CHAT_WORKERS` | 否 | RDS AI 流式请求线程池大小，默认 `8`。 |
| `RDS_BRIDGE_RESTART_BASE_SECONDS` | 否 | bridge 异常退出后的重启退避起始时间，默认 `3` 秒。 |
| `RDS_BRIDGE_RESTART_MAX_SECONDS` | 否 | bridge 异常退出后的最大重启退避时间，默认 `60` 秒。 |

**安全控制默认拒绝未授权用户**。可以配置对应平台的 `*_ALLOWED_USERS` 做访问控制；如果希望机器人对所有用户开放，可设置 `*_ALLOW_ALL_USERS=true` 或 `GATEWAY_ALLOW_ALL_USERS=true`。

| 变量名 | 必选 | 说明 |
|---|---|---|
| `GATEWAY_ALLOWED_USERS` | 否 | 全局允许访问的用户 ID，多个用逗号分隔。 |
| `GATEWAY_ALLOW_ALL_USERS` | 否 | 设为 `true` 时允许所有平台用户访问。 |

## 钉钉

在 [钉钉开放平台](https://open.dingtalk.com/) 创建应用并添加机器人，消息接收模式选择 Stream 模式。应用权限至少开通：企业内机器人发送消息权限、互动卡片实例写权限、AI 卡片流式更新权限。

```bash
cat > .env <<'EOF'
RDS_BOT_BRIDGES=dingtalk
ACCESS_KEY_ID=your-alibaba-cloud-access-key-id
ACCESS_SECRET=your-alibaba-cloud-access-key-secret

DINGTALK_APP_CLIENT_ID=your-dingtalk-client-id
DINGTALK_APP_CLIENT_SECRET=your-dingtalk-client-secret
DINGTALK_ALLOWED_USERS=sender-id-1,sender-staff-id-2
# 如需允许所有钉钉用户访问：DINGTALK_ALLOW_ALL_USERS=true
EOF

python main.py
```

| 变量名 | 必选 | 说明 |
|---|---|---|
| `DINGTALK_APP_CLIENT_ID` | 是 | 钉钉应用 Client ID，来自应用基础信息。 |
| `DINGTALK_APP_CLIENT_SECRET` | 是 | 钉钉应用 Client Secret，来自应用基础信息。 |
| `DINGTALK_ALLOWED_USERS` | 是（或 allow-all） | 允许访问的 `sender_id` 或 `sender_staff_id`，多个用逗号分隔；如已配置 allow-all 可不填。 |
| `DINGTALK_ALLOW_ALL_USERS` | 否 | 设为 `true` 时允许所有钉钉用户。 |
| `DINGTALK_ROBOT_CODE` | 否 | 钉钉机器人 RobotCode，默认使用 `DINGTALK_APP_CLIENT_ID`。 |
| `DINGTALK_ALLOWED_CHATS` | 否 | 允许访问的钉钉会话 ID，多个用逗号分隔。 |
| `DINGTALK_REQUIRE_MENTION` | 否 | 群聊是否要求 @ 或命中唤醒词，默认不强制。 |
| `DINGTALK_FREE_RESPONSE_CHATS` | 否 | 不要求 @ 的钉钉会话 ID，多个用逗号分隔。 |
| `DINGTALK_MENTION_PATTERNS` | 否 | 群聊唤醒词，多个用逗号分隔。 |

## 飞书

在 [飞书开放平台](https://open.feishu.cn/) 创建应用，启用机器人能力，并订阅 `im.message.receive_v1` 事件。当前只考虑长连接模式。

```bash
cat > .env <<'EOF'
RDS_BOT_BRIDGES=feishu
ACCESS_KEY_ID=your-alibaba-cloud-access-key-id
ACCESS_SECRET=your-alibaba-cloud-access-key-secret

FEISHU_APP_ID=your-feishu-app-id
FEISHU_APP_SECRET=your-feishu-app-secret
FEISHU_ALLOWED_USERS=open-id-1,union-id-2
# 如需允许所有飞书用户访问：FEISHU_ALLOW_ALL_USERS=true
# 国际版 Lark 可设置：FEISHU_DOMAIN=lark
EOF

python main.py
```

| 变量名 | 必选 | 说明 |
|---|---|---|
| `FEISHU_APP_ID` | 是 | 飞书应用 App ID，来自凭证与基础信息。 |
| `FEISHU_APP_SECRET` | 是 | 飞书应用 App Secret，来自凭证与基础信息。 |
| `FEISHU_ALLOWED_USERS` | 是（或 allow-all） | 允许访问的 `open_id`、`user_id` 或 `union_id`，多个用逗号分隔；如已配置 allow-all 可不填。 |
| `FEISHU_ALLOW_ALL_USERS` | 否 | 设为 `true` 时允许所有飞书用户。 |
| `FEISHU_DOMAIN` | 否 | 默认 `feishu`；国际版可配置为 `lark`。 |
| `FEISHU_GROUP_POLICY` | 否 | 群聊策略，默认 `mention`；支持 `open`、`mention`、`disabled`。 |
| `FEISHU_REQUIRE_MENTION` | 否 | 群聊是否要求 @ 机器人，默认随 `FEISHU_GROUP_POLICY` 判断。 |
| `FEISHU_ALLOW_BOTS` | 否 | 是否允许 bot sender 触发对话，默认不允许。 |
| `FEISHU_BOT_OPEN_ID` | 否 | 机器人 open_id，用于识别群聊 @。 |
| `FEISHU_BOT_USER_ID` | 否 | 机器人 user_id，用于识别群聊 @。 |
| `FEISHU_BOT_NAME` | 否 | 机器人名称，用于识别文本 @。 |

## 企业微信 WeCom AI Bot

在 [企业微信管理后台](https://work.weixin.qq.com/wework_admin/frame#/apps) 安全与管理 -> 管理工具 -> 智能机器人 里面创建或配置智能机器人，获取 Bot ID 和 Secret。当前支持 WeCom AI Bot WebSocket 长连接模式。

```bash
cat > .env <<'EOF'
RDS_BOT_BRIDGES=wecom
ACCESS_KEY_ID=your-alibaba-cloud-access-key-id
ACCESS_SECRET=your-alibaba-cloud-access-key-secret

WECOM_BOT_ID=your-wecom-ai-bot-id
WECOM_SECRET=your-wecom-ai-bot-secret
WECOM_ALLOWED_USERS=wecom-userid-1,wecom-userid-2
# 如需允许所有企业微信用户访问：WECOM_ALLOW_ALL_USERS=true
EOF

python main.py
```

| 变量名 | 必选 | 说明 |
|---|---|---|
| `WECOM_BOT_ID` | 是 | 企业微信 WeCom AI Bot ID。 |
| `WECOM_SECRET` | 是 | 企业微信 WeCom AI Bot Secret。 |
| `WECOM_ALLOWED_USERS` | 是（或 allow-all） | 允许访问的 `from.userid`，多个用逗号分隔；如已配置 allow-all 可不填。 |
| `WECOM_ALLOW_ALL_USERS` | 否 | 设为 `true` 时允许所有企业微信用户。 |
| `WECOM_WEBSOCKET_URL` | 否 | 企业微信 AI Bot WebSocket 网关，默认 `wss://openws.work.weixin.qq.com`。 |
| `WECOM_HEARTBEAT_SECONDS` | 否 | 企业微信应用层心跳间隔，默认 `30` 秒。 |
| `WECOM_RECONNECT_BASE_SECONDS` | 否 | 企业微信断线重连退避起始时间，默认 `3` 秒。 |
| `WECOM_RECONNECT_MAX_SECONDS` | 否 | 企业微信断线重连最大退避时间，默认 `60` 秒。 |
| `WECOM_DM_POLICY` | 否 | 单聊策略，默认 `open`；支持 `open`、`allowlist`、`disabled`。 |
| `WECOM_GROUP_POLICY` | 否 | 群聊策略，默认 `open`；支持 `open`、`allowlist`、`disabled`。 |
| `WECOM_ALLOWED_CHATS` | 否 | 群聊 allowlist 策略下允许的会话 ID，多个用逗号分隔。 |

## QQ Bot

在 [QQ 机器人开放平台](https://bot.q.qq.com/) 创建机器人，获取 AppID 和 Client Secret。接口和网关能力参考 [QQ Bot API v2 文档](https://bot.q.qq.com/wiki/develop/api-v2/)。

```bash
cat > .env <<'EOF'
RDS_BOT_BRIDGES=qqbot
ACCESS_KEY_ID=your-alibaba-cloud-access-key-id
ACCESS_SECRET=your-alibaba-cloud-access-key-secret

QQ_APP_ID=your-qq-bot-app-id
QQ_CLIENT_SECRET=your-qq-bot-client-secret
QQ_ALLOWED_USERS=user-openid-1,user-openid-2
# 如需允许所有 QQ 用户访问：QQ_ALLOW_ALL_USERS=true
EOF

python main.py
```

| 变量名 | 必选 | 说明 |
|---|---|---|
| `QQ_APP_ID` | 是 | QQ Bot AppID。 |
| `QQ_CLIENT_SECRET` | 是 | QQ Bot Client Secret。 |
| `QQ_ALLOWED_USERS` | 是（或 allow-all） | 允许访问的用户 OpenID，多个用逗号分隔；如已配置 allow-all 可不填。 |
| `QQ_ALLOW_ALL_USERS` | 否 | 设为 `true` 时允许所有 QQ 用户。 |
| `QQ_GROUP_ALLOWED_USERS` | 否 | 允许访问的 QQ 群 OpenID，多个用逗号分隔。 |
| `QQ_RECONNECT_BASE_SECONDS` | 否 | QQ Bot 网关断线重连退避起始时间，默认 `3` 秒。 |
| `QQ_RECONNECT_MAX_SECONDS` | 否 | QQ Bot 网关断线重连最大退避时间，默认 `60` 秒。 |
| `QQ_HTTP_VERIFY` | 否 | QQ Bot HTTPS/WSS 证书校验开关，默认 `true`；本地代理证书异常时可临时设为 `false`。 |
| `QQ_DM_POLICY` | 否 | 单聊策略，默认 `open`；支持 `open`、`disabled`。 |
| `QQ_GROUP_POLICY` | 否 | 群聊策略，默认 `open`；支持 `open`、`disabled`。 |

## 短命令

| 命令 | 说明 |
|---|---|
| `/help` | 查看短命令帮助。 |
| `/btw` | 查看当前正在运行任务已经收到的回复内容。 |
| `/stop` | 停止当前正在运行的 RDS AI 任务。 |
| `/session` | 查看当前多轮状态；开启时返回当前 `ConversationId`。 |
| `/session on` | 开启多轮对话保持（默认）。 |
| `/session off` | 关闭多轮对话保持，并清除当前保存的 `ConversationId`。 |
| `/session ls` | 拉取最近的 RDS AI 对话列表。 |
| `/session <id>` | 切换到指定对话，`id` 支持完整 ID 或最近列表中的 8 位短 ID。 |
| `/new` | 清除当前 `ConversationId`，下一条普通消息开启新对话。 |
| `/agent` | 查看当前会话绑定的 Custom Agent；未绑定时显示默认 RDS Copilot。 |
| `/agent ls` | 拉取 Custom Agent 列表。 |
| `/agent <agent-name>` | 使用最近列表中的 Agent 名称切换当前会话的 Custom Agent。 |
| `/agent default` | 清除当前 Custom Agent，恢复默认 RDS Copilot。 |
| `/language` | 查看可选语言：`zh-CN`（默认）、`zh-TW`、`en-US`、`ja-JP`。 |
| `/language <language>` | 切换当前会话语言，例如 `/language en-US`；大小写和 `_` 会自动规范化。 |
| `/tz` | 查看当前时区，默认 `Asia/Shanghai`。 |
| `/tz <timezone>` | 切换当前会话时区，例如 `/tz Asia/Shanghai`；仅接受本机存在的 IANA 时区。 |
| `/skills` | 按当前语言拉取 Skill 列表，默认第 1 页。 |
| `/skills <page>` | 拉取指定页 Skill 列表，例如 `/skills 2`。 |
| `/card` | 钉钉查看当前卡片回复状态。 |
| `/card on` | 钉钉开启 AI 卡片回复。 |
| `/card off` | 钉钉关闭 AI 卡片回复（默认），改为普通消息回复。 |

## 目录结构

```text
rds-copilot-bot-gateway/
  main.py
  core/
  bridges/
  scripts/
  tests/
```

## 参考链接

- [阿里云 RDS AI 助手](https://help.aliyun.com/zh/rds/)
- [ChatMessages API](https://help.aliyun.com/zh/rds/developer-reference/api-rdsai-2025-05-07-chatmessages)
- [GetConversations API](https://help.aliyun.com/zh/rds/developer-reference/api-rdsai-2025-05-07-getconversations)
- [ListCustomAgent API](https://help.aliyun.com/zh/rds/developer-reference/api-rdsai-2025-05-07-listcustomagent)
- [ListSkill API](https://help.aliyun.com/zh/rds/developer-reference/api-rdsai-2025-05-07-listskill)
- [钉钉开放平台](https://open.dingtalk.com/)
- [飞书开放平台](https://open.feishu.cn/)
- [企业微信开发者中心](https://developer.work.weixin.qq.com/)
- [QQ Bot API v2 文档](https://bot.q.qq.com/wiki/develop/api-v2/)
