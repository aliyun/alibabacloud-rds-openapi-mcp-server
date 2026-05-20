# 钉钉机器人接入 RDS AI 助手

本目录为 **RDS AI 助手专业版** 钉钉机器人接入的示例代码，支持在钉钉 APP 内通过与机器人对话的方式，使用 RDS AI 助手进行 RDS 实例的智能管理与运维。

> **重要**：仅 RDS AI 助手专业版支持钉钉机器人接入，配置前请先购买 [阿里云 RDS AI 助手专业版](https://help.aliyun.com/zh/rds/)。

## 流程概述

1. **创建并配置钉钉机器人**：在钉钉开放平台创建应用与机器人，完成权限配置与应用发布。
2. **配置服务端环境并启动服务**：安装依赖、配置环境变量并运行本示例。
3. **在钉钉内使用**：在群组或单聊中与机器人对话使用 RDS AI 助手。

---

## 一、创建、配置与发布钉钉机器人

### 1. 创建应用

1. 登录 [钉钉开放平台](https://open.dingtalk.com/)，右上角进入 **开发者后台**。
2. 在 **应用开发 > 钉钉应用** 中点击 **创建应用**。
3. 填写应用名称、应用描述与应用图标（可选），点击 **确定** 创建。

### 2. 添加并配置机器人

1. 应用创建完成后，点击 **添加机器人**。
2. 在机器人配置页填写：机器人名称、机器人图标、简介、功能描述。
3. **消息接收模式** 选择 **Stream 模式**，然后点击 **发布**。

### 3. 配置权限

在左侧 **权限管理** 中为应用申请以下权限并 **立即开通**：

- **企业内机器人发送消息权限**
- **互动卡片实例写权限**
- **AI 卡片流式更新权限**

### 4. 发布应用版本

1. 在左侧 **版本管理与发布** 中点击 **创建新版本**。
2. 填写版本描述，选择应用可用范围，点击 **直接发布**。
3. 发布完成后版本状态为 **已上线**。

---

## 二、配置服务端环境

### 1. 安装依赖

在项目根目录下执行：

```bash
cd "ecological integration/dingtalk"
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 环境变量

启动前需配置以下环境变量。

| 变量名 | 说明 | 获取方式 |
|--------|------|----------|
| `ACCESS_KEY_ID` | 阿里云 AccessKey ID | [阿里云控制台](https://www.aliyun.com/) → 创建/使用已有 AccessKey，建议使用 RAM 用户以降低主账号风险 |
| `ACCESS_SECRET` | 阿里云 AccessKey Secret | 同上，创建时仅显示一次，请妥善保管 |
| `DINGTALK_APP_CLIENT_ID` | 钉钉应用 Client ID | [钉钉开放平台](https://open.dingtalk.com/) → 进入应用 → **基础信息** → 凭证与基础信息 |
| `DINGTALK_APP_CLIENT_SECRET` | 钉钉应用 Client Secret | 同上 |

配置示例（Linux/macOS）：

```bash
export ACCESS_KEY_ID="your_aliyun_access_key_id"
export ACCESS_SECRET="your_aliyun_access_key_secret"
export DINGTALK_APP_CLIENT_ID="your_dingtalk_client_id"
export DINGTALK_APP_CLIENT_SECRET="your_dingtalk_client_secret"
```

不要通过命令行传入 `DINGTALK_APP_CLIENT_SECRET`，避免 secret 出现在 shell history 或进程列表中。

### 3. 启动服务

```bash
python main.py
```

服务启动后，钉钉侧消息会转发到本示例，并调用 RDS AI 助手接口进行对话与卡片流式更新。

---

## 三、在钉钉内使用 RDS AI 助手

### 群组内使用

1. 在钉钉 APP 中进入目标群组 → **群设置** → **机器人** → **添加机器人**。
2. 搜索并添加你在第一步创建的机器人。
3. 在群里 **@ 机器人名称** 后输入问题即可对话。

### 单聊使用

1. 在钉钉 APP 顶部搜索框中输入该机器人名称，进入聊天界面。
2. 直接输入问题并发送即可。

---

## 项目结构

| 文件 | 说明 |
|------|------|
| `main.py` | 钉钉 Stream 模式消息接收与转发、卡片流式更新、与 RDS Copilot 的调用封装 |
| `rds_copilot.py` | RDS AI 助手（Copilot）OpenAPI 客户端，封装对话、工具调用与文档事件等 |
| `requirements.txt` | Python 依赖（dingtalk-stream、dashscope、阿里云 OpenAPI SDK 等） |

---

## 参考文档

- [钉钉机器人接入 RDS AI 助手 - 阿里云帮助中心](https://help.aliyun.com/zh/rds/apsaradb-rds-for-mysql/integrate-rds-copilot-into-a-dingtalk-bot)
- [钉钉开放平台](https://open.dingtalk.com/)
- [创建 AccessKey](https://help.aliyun.com/zh/ram/user-guide/create-an-accesskey-pair)
