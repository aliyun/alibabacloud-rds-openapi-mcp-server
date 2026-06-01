# 钉钉/飞书/企业微信/QQ Bot 接入 RDS AI 助手

本目录提供 RDS AI 助手机器人接入示例，支持通过长连接模式接入钉钉、飞书、企业微信 WeCom AI Bot 和 QQ Bot。配置完成后，用户可以在 IM 单聊或群聊里直接向机器人提问，机器人会调用 RDS AI 助手返回结果。

> 仅 RDS AI 助手专业版支持机器人接入。配置前请先开通 RDS AI 助手专业版，并准备好阿里云 AccessKey。

## 选择启动方式

推荐使用 Docker 部署；如果要在本机开发调试，也可以直接用 Python 启动。两种方式使用同一组环境变量，只是 `.env` 文件位置不同：

| 启动方式 | `.env` 位置 | 日志和会话状态 |
|---|---|---|
| Docker | `rds-copilot-bot-gateway/docker/.env` | `rds-copilot-bot-gateway/docker/data/` |
| 本地 Python | `rds-copilot-bot-gateway/.env` | 当前运行目录，或由环境变量指定 |

下面各 IM 平台章节里的配置片段，写入你选择的 `.env` 文件即可。

### Docker 启动

```bash
cd rds-copilot-bot-gateway/docker
# 在当前目录创建 .env，并填入下面的通用配置和平台配置
# 如果已经有本地 Python 启动用的 .env，也可以执行：cp ../.env .env
docker build -t rds-copilot-bot-gateway:latest -f Dockerfile ..
docker compose up -d
```

查看日志：

```bash
docker compose logs -f
tail -f data/rds-copilot.log
```

### 本地 Python 启动

```bash
cd rds-copilot-bot-gateway
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

### 同时启动多个平台

同时启动多个平台时，在 `.env` 中设置：

```dotenv
RDS_BOT_BRIDGES=dingtalk,feishu
# 或启动全部平台
RDS_BOT_BRIDGES=all
```

## 通用环境变量

| 变量名 | 必选 | 示例值 | 默认值 | 说明 |
|---|---|---|---|---|
| `ACCESS_KEY_ID` | 是 | `LTAIxxxxxxxxxxxxxxxx` | 无 | 阿里云 AccessKey ID，可在 [RAM 控制台](https://help.aliyun.com/zh/ram/user-guide/create-an-accesskey-pair) 创建，建议使用 RAM 用户。 |
| `ACCESS_SECRET` | 是 | `example-access-key-secret` | 无 | 阿里云 AccessKey Secret，创建后仅显示一次。 |
| `RDS_COPILOT_ENDPOINT` | 否 | `rdsai.aliyuncs.com` | `rdsai.aliyuncs.com` | RDS AI OpenAPI Endpoint。 |
| `RDS_BOT_BRIDGES` | 否 | `dingtalk,feishu` | `dingtalk` | 启动的平台；`dingtalk` 表示钉钉，`feishu` 表示飞书，`wecom` 表示企业微信，`qqbot` 表示 QQ Bot，多个用逗号分隔，`all` 表示全部启动。 |
| `RDS_COPILOT_LOG_FILE` | 否 | `/data/rds-copilot.log` | 本地：`rds-copilot.log`；Docker：`/data/rds-copilot.log` | 日志文件路径。 |
| `RDS_COPILOT_CONVERSATION_STORE_FILE` | 否 | `/data/copilot_conversations.json` | 本地：`copilot_conversations.json`；Docker：`/data/copilot_conversations.json` | 会话状态 JSON 文件路径。 |
| `RDS_BOT_STILL_WORKING_INTERVAL_SECONDS` | 否 | `180` | `180` | 长任务运行中提示间隔，单位秒；设为 `0` 表示关闭长任务提示。 |
| `RDS_COPILOT_CHAT_WORKERS` | 否 | `8` | `8` | RDS AI 流式请求线程池大小。 |
| `RDS_BRIDGE_RESTART_BASE_SECONDS` | 否 | `3` | `3` | bridge 异常退出后的重启退避起始时间，单位秒。 |
| `RDS_BRIDGE_RESTART_MAX_SECONDS` | 否 | `60` | `60` | bridge 异常退出后的最大重启退避时间，单位秒。 |
| `RDS_COPILOT_LOG_PAYLOADS` | 否 | `false` | `false` | 是否记录 RDS AI 原始流式 payload；`true` 表示开启，`false` 或不填表示关闭。 |

**安全控制默认拒绝未授权普通对话**。每个平台都用同一套安全变量：

| 后缀 | 默认值 | 说明 |
|---|---|---|
| `DM_ALLOW_POLICY` | `allowlist` | 单聊策略；`disabled` 表示不处理单聊，`allowlist` 表示只允许名单内用户，`open` 表示允许所有单聊用户。 |
| `DM_ALLOW_LIST` | 空 | 单聊用户 ID 列表，多个用逗号分隔。 |
| `GROUP_ALLOW_POLICY` | `allowlist` | 群聊策略；`disabled` 表示不处理群聊，`allowlist` 表示只允许名单内群聊，`open` 表示允许所有群聊。 |
| `GROUP_ALLOW_LIST` | 空 | 群聊会话 ID 列表，多个用逗号分隔。 |

首次配置时，可以临时把对应平台的 `DM_ALLOW_POLICY` 和 `GROUP_ALLOW_POLICY` 设为 `open`，在单聊和群聊里发送 `/myid` 或 `$myid`，机器人会返回当前平台的用户 ID、群聊 ID 和可复制的配置片段。拿到 ID 后建议改回 `allowlist`。

## 获取用户 ID 和群聊 ID

IM App 里能看到的昵称、手机号、QQ 号或群名称，通常不是开放平台消息里的真实 ID。建议用下面方式获取：

1. 先临时设置对应平台的 `DM_ALLOW_POLICY=open` 和 `GROUP_ALLOW_POLICY=open`。
2. 在单聊和目标群聊里发送 `/myid` 或 `$myid`。
3. 把机器人返回的 `*_ALLOW_LIST` 配置复制到 `.env`。
4. 把策略改回 `allowlist` 后重启服务。

## 钉钉

在 [钉钉开放平台](https://open.dingtalk.com/) 创建应用并添加机器人，消息接收模式选择 Stream 模式。应用权限至少开通：企业内机器人发送消息权限、互动卡片实例写权限、AI 卡片流式更新权限。

`.env` 示例：

```dotenv
RDS_BOT_BRIDGES=dingtalk
ACCESS_KEY_ID=your-alibaba-cloud-access-key-id
ACCESS_SECRET=your-alibaba-cloud-access-key-secret

DINGTALK_APP_CLIENT_ID=your-dingtalk-client-id
DINGTALK_APP_CLIENT_SECRET=your-dingtalk-client-secret
DINGTALK_DM_ALLOW_POLICY=allowlist
DINGTALK_DM_ALLOW_LIST=0145175824431637433425
DINGTALK_GROUP_ALLOW_POLICY=allowlist
DINGTALK_GROUP_ALLOW_LIST=cidxxxxxxxx
```

| 变量名 | 必选 | 示例值 | 默认值 | 说明 |
|---|---|---|---|---|
| `DINGTALK_APP_CLIENT_ID` | 是 | `dingxxxxxxxxxxxxxxxx` | 无 | 钉钉应用 Client ID，来自应用基础信息。 |
| `DINGTALK_APP_CLIENT_SECRET` | 是 | `example-dingtalk-client-secret` | 无 | 钉钉应用 Client Secret，来自应用基础信息。 |
| `DINGTALK_DM_ALLOW_POLICY` | 否 | `allowlist` | `allowlist` | 单聊安全策略；`disabled` 不处理单聊，`allowlist` 只允许 `DINGTALK_DM_ALLOW_LIST`，`open` 允许所有单聊。 |
| `DINGTALK_DM_ALLOW_LIST` | 否 | `0145175824431637433425,sender-id-1` | 空 | 允许访问的钉钉单聊用户 ID；优先使用 `/myid` 返回的用户 ID，钉钉会优先展示 `senderStaffId`。 |
| `DINGTALK_GROUP_ALLOW_POLICY` | 否 | `allowlist` | `allowlist` | 群聊安全策略；`disabled` 不处理群聊，`allowlist` 只允许 `DINGTALK_GROUP_ALLOW_LIST`，`open` 允许所有群聊。 |
| `DINGTALK_GROUP_ALLOW_LIST` | 否 | `cidxxxxxxxx,cidyyyyyyyy` | 空 | 允许访问的钉钉群聊会话 ID。 |
| `DINGTALK_ROBOT_CODE` | 否 | `dingxxxxxxxxxxxxxxxx` | `DINGTALK_APP_CLIENT_ID` | 钉钉机器人 RobotCode。 |
| `DINGTALK_REQUIRE_MENTION` | 否 | `true` | `false` | 群聊是否要求 @ 或命中唤醒词；`true` 表示群聊必须 @ 机器人或命中 `DINGTALK_MENTION_PATTERNS`，`false` 或不填表示不强制。 |
| `DINGTALK_FREE_RESPONSE_CHATS` | 否 | `cidxxxx` | 空 | 不要求 @ 的钉钉会话 ID，多个用逗号分隔；仅在 `DINGTALK_REQUIRE_MENTION=true` 时生效。 |
| `DINGTALK_MENTION_PATTERNS` | 否 | `RDS助手,小RDS` | 空 | 群聊唤醒词，多个用逗号分隔；命中任一子串即可触发，大小写不敏感。 |

## 飞书

在 [飞书开放平台](https://open.feishu.cn/) 创建应用，启用机器人能力，并订阅 `im.message.receive_v1` 事件。支持长连接模式。

`.env` 示例：

```dotenv
RDS_BOT_BRIDGES=feishu
ACCESS_KEY_ID=your-alibaba-cloud-access-key-id
ACCESS_SECRET=your-alibaba-cloud-access-key-secret

FEISHU_APP_ID=your-feishu-app-id
FEISHU_APP_SECRET=your-feishu-app-secret
FEISHU_DM_ALLOW_POLICY=allowlist
FEISHU_DM_ALLOW_LIST=ou_xxxxxxxxx,on_xxxxxxxxx
FEISHU_GROUP_ALLOW_POLICY=allowlist
FEISHU_GROUP_ALLOW_LIST=oc_xxxxxxxxx
# 国际版 Lark 可设置：FEISHU_DOMAIN=lark
```

| 变量名 | 必选 | 示例值 | 默认值 | 说明 |
|---|---|---|---|---|
| `FEISHU_APP_ID` | 是 | `cli_xxxxxxxxxxxxxxxx` | 无 | 飞书应用 App ID，来自凭证与基础信息。 |
| `FEISHU_APP_SECRET` | 是 | `example-feishu-app-secret` | 无 | 飞书应用 App Secret，来自凭证与基础信息。 |
| `FEISHU_DM_ALLOW_POLICY` | 否 | `allowlist` | `allowlist` | 单聊安全策略；`disabled` 不处理单聊，`allowlist` 只允许 `FEISHU_DM_ALLOW_LIST`，`open` 允许所有单聊。 |
| `FEISHU_DM_ALLOW_LIST` | 否 | `ou_xxx,on_xxx` | 空 | 允许访问的飞书单聊用户 ID，支持 `open_id`、`user_id` 或 `union_id`。 |
| `FEISHU_GROUP_ALLOW_POLICY` | 否 | `allowlist` | `allowlist` | 群聊安全策略；`disabled` 不处理群聊，`allowlist` 只允许 `FEISHU_GROUP_ALLOW_LIST`，`open` 允许所有群聊。 |
| `FEISHU_GROUP_ALLOW_LIST` | 否 | `oc_xxx,oc_yyy` | 空 | 允许访问的飞书群聊 `chat_id`。 |
| `FEISHU_DOMAIN` | 否 | `feishu` | `feishu` | 飞书域名类型；`feishu` 表示中国区飞书，`lark` 表示国际版 Lark。 |
| `FEISHU_GROUP_POLICY` | 否 | `mention` | `mention` | 群聊策略；`open` 表示群聊不要求 @，`mention` 表示群聊要求 @ 或识别到机器人标识，`disabled` 表示不处理群聊。 |
| `FEISHU_ALLOW_BOTS` | 否 | `false` | `false` | 是否允许 bot sender 触发对话；`true` 表示允许，`false` 或不填表示忽略 bot sender。 |
| `FEISHU_BOT_OPEN_ID` | 否 | `ou_bot_xxx` | 空 | 机器人 open_id，用于识别群聊 @；通常启动时会自动获取机器人身份。 |
| `FEISHU_BOT_USER_ID` | 否 | `u_bot_xxx` | 空 | 机器人 user_id，用于识别群聊 @；通常启动时会自动获取机器人身份。 |
| `FEISHU_BOT_NAME` | 否 | `RDS助手` | 空 | 机器人名称，用于识别文本 @；通常启动时会自动获取机器人身份。 |

## 企业微信 WeCom AI Bot

在 [企业微信管理后台](https://work.weixin.qq.com/wework_admin/frame#/apps) 安全与管理 -> 管理工具 -> 智能机器人 里面创建或配置智能机器人，获取 Bot ID 和 Secret。支持 WeCom AI Bot WebSocket 长连接模式。

`.env` 示例：

```dotenv
RDS_BOT_BRIDGES=wecom
ACCESS_KEY_ID=your-alibaba-cloud-access-key-id
ACCESS_SECRET=your-alibaba-cloud-access-key-secret

WECOM_BOT_ID=your-wecom-ai-bot-id
WECOM_SECRET=your-wecom-ai-bot-secret
WECOM_DM_ALLOW_POLICY=allowlist
WECOM_DM_ALLOW_LIST=zhangsan,lisi
WECOM_GROUP_ALLOW_POLICY=allowlist
WECOM_GROUP_ALLOW_LIST=wrsxxxxxxxx
```

| 变量名 | 必选 | 示例值 | 默认值 | 说明 |
|---|---|---|---|---|
| `WECOM_BOT_ID` | 是 | `aibxxxxxxxxxxxxxxxx` | 无 | 企业微信 WeCom AI Bot ID。 |
| `WECOM_SECRET` | 是 | `example-wecom-secret` | 无 | 企业微信 WeCom AI Bot Secret。 |
| `WECOM_DM_ALLOW_POLICY` | 否 | `allowlist` | `allowlist` | 单聊安全策略；`disabled` 不处理单聊，`allowlist` 只允许 `WECOM_DM_ALLOW_LIST`，`open` 允许所有单聊。 |
| `WECOM_DM_ALLOW_LIST` | 否 | `zhangsan,lisi` | 空 | 允许访问的企业微信单聊用户 ID，对应消息里的 `from.userid`。 |
| `WECOM_GROUP_ALLOW_POLICY` | 否 | `allowlist` | `allowlist` | 群聊安全策略；`disabled` 不处理群聊，`allowlist` 只允许 `WECOM_GROUP_ALLOW_LIST`，`open` 允许所有群聊。 |
| `WECOM_GROUP_ALLOW_LIST` | 否 | `wrsxxxxxxxx,wrsyyyyyyyy` | 空 | 允许访问的企业微信群聊 `chatid`。 |
| `WECOM_WEBSOCKET_URL` | 否 | `wss://openws.work.weixin.qq.com` | `wss://openws.work.weixin.qq.com` | 企业微信 AI Bot WebSocket 网关。 |
| `WECOM_HEARTBEAT_SECONDS` | 否 | `30` | `30` | 企业微信应用层心跳间隔，单位秒。 |
| `WECOM_RECONNECT_BASE_SECONDS` | 否 | `3` | `3` | 企业微信断线重连退避起始时间，单位秒。 |
| `WECOM_RECONNECT_MAX_SECONDS` | 否 | `60` | `60` | 企业微信断线重连最大退避时间，单位秒。 |

## QQ Bot

在 [QQ 机器人开放平台](https://bot.q.qq.com/) 创建机器人，获取 AppID 和 Client Secret。接口和网关能力参考 [QQ Bot API v2 文档](https://bot.q.qq.com/wiki/develop/api-v2/)。

`.env` 示例：

```dotenv
RDS_BOT_BRIDGES=qqbot
ACCESS_KEY_ID=your-alibaba-cloud-access-key-id
ACCESS_SECRET=your-alibaba-cloud-access-key-secret

QQ_APP_ID=your-qq-bot-app-id
QQ_CLIENT_SECRET=your-qq-bot-client-secret
QQ_DM_ALLOW_POLICY=allowlist
QQ_DM_ALLOW_LIST=user-openid-1,user-openid-2
QQ_GROUP_ALLOW_POLICY=allowlist
QQ_GROUP_ALLOW_LIST=group-openid-1
```

| 变量名 | 必选 | 示例值 | 默认值 | 说明 |
|---|---|---|---|---|
| `QQ_APP_ID` | 是 | `1900000000` | 无 | QQ Bot AppID。 |
| `QQ_CLIENT_SECRET` | 是 | `example-qq-client-secret` | 无 | QQ Bot Client Secret。 |
| `QQ_DM_ALLOW_POLICY` | 否 | `allowlist` | `allowlist` | 单聊安全策略；`disabled` 不处理单聊，`allowlist` 只允许 `QQ_DM_ALLOW_LIST`，`open` 允许所有单聊。 |
| `QQ_DM_ALLOW_LIST` | 否 | `user-openid-1,user-openid-2` | 空 | 允许访问的 QQ 单聊用户 OpenID。 |
| `QQ_GROUP_ALLOW_POLICY` | 否 | `allowlist` | `allowlist` | 群聊安全策略；`disabled` 不处理群聊，`allowlist` 只允许 `QQ_GROUP_ALLOW_LIST`，`open` 允许所有群聊。 |
| `QQ_GROUP_ALLOW_LIST` | 否 | `group-openid-1,channel-id-1` | 空 | 允许访问的 QQ 群 OpenID；频道场景可填 `channel_id`。 |
| `QQ_RECONNECT_BASE_SECONDS` | 否 | `3` | `3` | QQ Bot 网关断线重连退避起始时间，单位秒。 |
| `QQ_RECONNECT_MAX_SECONDS` | 否 | `60` | `60` | QQ Bot 网关断线重连最大退避时间，单位秒。 |

## 短命令

短命令支持 `/` 或 `$` 前缀，例如 `/session` 和 `$session` 等价。

| 命令 | 说明 |
|---|---|
| `/help` | 查看短命令帮助。 |
| `/myid` | 查看当前 IM 平台的用户 ID、群聊 ID 和安全控制配置片段。 |
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

## 群聊高级配置

群聊回复、短命令回复和长任务提醒会在群聊里 @ 发送人；单聊不会额外 @。群聊能否使用由各平台的 `GROUP_ALLOW_POLICY` 和 `GROUP_ALLOW_LIST` 控制，钉钉和飞书还可以额外配置是否要求 @ 机器人。

如果你还不知道群聊 ID，先临时设置对应平台 `GROUP_ALLOW_POLICY=open`，在群里发送 `/myid`，拿到 ID 后再改为 `allowlist`。

### 钉钉群聊

| 想要的效果 | 配置组合 | 结果 |
|---|---|---|
| 任意群直接提问都会回复 | `DINGTALK_GROUP_ALLOW_POLICY=open`，`DINGTALK_REQUIRE_MENTION=false` | 不限制群 ID，也不要求 @ 或唤醒词。 |
| 只允许指定群使用，群内不要求 @ | `DINGTALK_GROUP_ALLOW_POLICY=allowlist`，`DINGTALK_GROUP_ALLOW_LIST=cidA,cidB`，`DINGTALK_REQUIRE_MENTION=false` | 只有 `cidA`、`cidB` 的消息会被处理。 |
| 任意群都必须 @ 机器人或命中唤醒词 | `DINGTALK_REQUIRE_MENTION=true`，`DINGTALK_MENTION_PATTERNS=RDS助手,小RDS` | 用户 @ 机器人，或消息里包含任一唤醒词时才回复。 |
| 只允许指定群，并且指定群里也要 @ 或唤醒词 | `DINGTALK_GROUP_ALLOW_POLICY=allowlist`，`DINGTALK_GROUP_ALLOW_LIST=cidA,cidB`，`DINGTALK_REQUIRE_MENTION=true`，`DINGTALK_MENTION_PATTERNS=RDS助手` | 先限制群 ID，再判断是否 @ 或命中唤醒词。 |
| 大部分群要求 @，少数群可以直接提问 | `DINGTALK_GROUP_ALLOW_POLICY=allowlist`，`DINGTALK_GROUP_ALLOW_LIST=cidA,cidB`，`DINGTALK_REQUIRE_MENTION=true`，`DINGTALK_FREE_RESPONSE_CHATS=cidA` | `cidA` 不要求 @；其他群仍要求 @ 或唤醒词。 |

### 飞书群聊

| 想要的效果 | 配置组合 | 结果 |
|---|---|---|
| 只允许指定群，且群聊只响应 @ 机器人 | `FEISHU_GROUP_ALLOW_POLICY=allowlist`，`FEISHU_GROUP_ALLOW_LIST=ocA,ocB`，`FEISHU_GROUP_POLICY=mention` | 推荐默认效果；群 ID 命中且 @ 机器人时才回复。 |
| 允许所有群，群里直接提问就回复 | `FEISHU_GROUP_ALLOW_POLICY=open`，`FEISHU_GROUP_POLICY=open` | 群聊不要求 @。 |
| 不处理任何群聊 | `FEISHU_GROUP_ALLOW_POLICY=disabled` | 群聊消息会被忽略，单聊不受影响。 |
| 使用 mention 策略但手动指定机器人身份 | `FEISHU_GROUP_POLICY=mention`，`FEISHU_BOT_OPEN_ID=ou_bot_xxx`，`FEISHU_BOT_NAME=RDS助手` | 当自动获取机器人身份失败，或群聊 @ 识别不稳定时使用。 |
| 允许其他 bot sender 触发对话 | `FEISHU_ALLOW_BOTS=true` | 默认会忽略 bot sender；只有明确开启后才处理。 |

### 企业微信群聊

| 想要的效果 | 配置组合 | 结果 |
|---|---|---|
| 任意群直接提问都会回复 | `WECOM_GROUP_ALLOW_POLICY=open` | 允许所有企业微信群聊。 |
| 只允许指定群使用 | `WECOM_GROUP_ALLOW_POLICY=allowlist`，`WECOM_GROUP_ALLOW_LIST=roomA,roomB` | 只有命中群聊 `chatid` 的消息会被处理。 |
| 不处理任何群聊 | `WECOM_GROUP_ALLOW_POLICY=disabled` | 群聊消息会被忽略，单聊由 `WECOM_DM_ALLOW_POLICY` 控制。 |

### QQ 群聊

| 想要的效果 | 配置组合 | 结果 |
|---|---|---|
| 处理 QQ 平台推送给机器人的群聊消息 | `QQ_GROUP_ALLOW_POLICY=open` | 允许所有 QQ 群聊。QQ 群消息通常由平台以 at-message 事件推送。 |
| 只允许指定 QQ 群使用 | `QQ_GROUP_ALLOW_POLICY=allowlist`，`QQ_GROUP_ALLOW_LIST=group-openid-1,group-openid-2` | 只有命中群 OpenID 的群聊会被处理。 |
| 不处理任何群聊 | `QQ_GROUP_ALLOW_POLICY=disabled` | 群聊消息会被忽略，单聊由 `QQ_DM_ALLOW_POLICY` 控制。 |

## 目录结构

```text
rds-copilot-bot-gateway/
  main.py
  core/
  bridges/
  docker/
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
