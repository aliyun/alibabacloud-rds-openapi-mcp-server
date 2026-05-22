<p align="center"><a href="./README.md">English</a> | 中文<br></p>

# 阿里云RDS OpenAPI MCP 服务
RDS OpenAPI MCP服务。
## 前提条件
1. 从[Astral](https://docs.astral.sh/uv/getting-started/installation/)或[GitHub README](https://github.com/astral-sh/uv#installation)安装`uv`
2. 使用`uv python install 3.12`安装Python
3. 具有阿里云RDS服务访问权限的账号凭证

## 快速开始
### 使用[cherry-studio](https://github.com/CherryHQ/cherry-studio)（推荐）
1. [下载](https://docs.cherry-ai.com/cherry-studio/download)并安装cherry-studio
2. 根据[文档](https://docs.cherry-ai.com/advanced-basic/mcp/install)安装MCP环境所需的uv
3. 根据[文档](https://docs.cherry-ai.com/advanced-basic/mcp/config) 配置和使用RDS MCP，使用下面的JSON可以快速导入RDS MCP配置。请将`ALIBABA_CLOUD_ACCESS_KEY_ID`和`ALIBABA_CLOUD_ACCESS_KEY_SECRET`配置成阿里云AKSK。

> 导入时可能会看到以下报错，可以忽略：
> xxx settings.mcp.addServer.importFrom.connectionFailed

<img src="./assets/cherry-config.png" alt="cherry_config"/>

```json5
{
	"mcpServers": {
		"rds-openapi": {
			"name": "rds-openapi",
			"type": "stdio",
			"description": "",
			"isActive": true,
			"registryUrl": "",
			"command": "uvx",
			"args": [
				"alibabacloud-rds-openapi-mcp-server@latest"
			],
			"env": {
				"ALIBABA_CLOUD_ACCESS_KEY_ID": "$you_access_id",
				"ALIBABA_CLOUD_ACCESS_KEY_SECRET": "$you_access_key"
			}
		}
	}
}
```

4. 最后点击开启MCP
<img src="./assets/mcp_turn_on.png" alt="mcp_turn_on"/>

5. 您可以使用我们下面提供的提示词模板，提升使用体验。


### 使用Cline
设置环境变量并运行MCP服务
```shell
# 设置环境变量
export SERVER_TRANSPORT=sse;
export SERVER_HOST=127.0.0.1; # 默认值：127.0.0.1。监听非本地地址时必须配置 API_KEY。
export ALIBABA_CLOUD_ACCESS_KEY_ID=$your_access_id;  # 替换为你的access_id
export ALIBABA_CLOUD_ACCESS_KEY_SECRET=$your_access_key;  # 替换为你的access_key
export ALIBABA_CLOUD_SECURITY_TOKEN=$your_sts_security_token; # 可选项，使用sts token鉴权时填写
export API_KEY=$you_mcp_server_api_key; # 当 SERVER_HOST 不是本地回环地址时必填。
export ENABLE_WRITE_TOOLS=false; # 仅在确认需要对非本地地址暴露写操作工具时设置为 true。
export ALLOW_HEADER_CREDENTIALS=false; # 默认 false。仅在可信部署中需要按请求传入凭证时设置为 true。

# 启动MCP服务
uvx alibabacloud-rds-openapi-mcp-server@latest
```
成功启动后会看到以下输出：
```shell
INFO:     Started server process [91594]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```
然后在Cline中配置：
```shell
remote_server = "http://127.0.0.1:8000/sse";
```
配置 `API_KEY` 后，MCP 客户端必须发送 `Authorization: Bearer <API_KEY>`。

> 如果使用Qwen遇到`401 Incorrect API key provided`错误，请参考[文档](https://help.aliyun.com/zh/model-studio/cline)解决。

### 使用Claude
从Github克隆仓库
```shell
git clone https://github.com/aliyun/alibabacloud-rds-openapi-mcp-server.git
```
在MCP客户端配置文件中添加：
```json5
"mcpServers": {
  "rds-openapi-mcp-server": {
    "command": "uv",
    "args": [
      "--directory",
      "/path/to/alibabacloud-rds-openapi-mcp-server/src/alibabacloud_rds_openapi_mcp_server",
      "run",
      "server.py"
    ],
    "env": {
      "ALIBABA_CLOUD_ACCESS_KEY_ID": "access_id",
      "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "access_key",
      "ALIBABA_CLOUD_SECURITY_TOKEN": "sts_security_token" // 可选项，使用sts token鉴权时填写
}
  }
}
```

## 功能组件
### OpenAPI 工具集
* `add_tags_to_db_instance`: 添加标签到RDS实例
* `allocate_instance_public_connection`: 为RDS实例分配公网连接
* `attach_whitelist_template_to_instance`: 将白名单模板绑定到RDS实例
* `create_db_instance`: 创建RDS实例
* `create_db_instance_account`: 创建RDS实例账号
* `describe_all_whitelist_template`: 查询白名单模板列表
* `describe_available_classes`: 查询可用实例规格和存储范围
* `describe_available_zones`: 查询RDS实例可用区域
* `describe_bills`: 查询用户在特定计费周期内所有产品实例或计费项的消费汇总
* `describe_db_instance_accounts`: 批量查询多个RDS实例的账户信息
* `describe_db_instance_attribute`: 查询实例详细信息
* `describe_db_instance_databases`: 批量查询多个RDS实例的数据库信息
* `describe_db_instance_ip_allowlist`: 批量查询多个RDS实例的IP白名单配置
* `describe_db_instance_net_info`: 批量查询多个RDS实例的网络配置详情
* `describe_db_instance_parameters`: 批量查询多个RDS实例的参数信息
* `describe_db_instance_performance`: 查询实例性能数据
* `describe_db_instances`: 查询实例
* `describe_error_logs`: 查询实例错误日志
* `describe_instance_linked_whitelist_template`: 查询绑定到实例的白名单模板列表
* `describe_monitor_metrics`: 通过 DAS API 查询RDS实例监控指标
* `describe_slow_log_records`: 查询RDS实例的慢日志记录
* `describe_sql_insight_statistic`: 查询实例SQL日志统计，包括SQL耗时、执行次数、账号等
* `describe_vpcs`: 查询VPC列表
* `modify_security_ips`: 修改白名单
* `describe_vswitches`: 查询VSwitch列表
* `get_current_time`: 获取当前时间
* `modify_db_instance_description`: 修改RDS实例描述
* `modify_db_instance_spec`: 修改RDS实例规格
* `modify_parameter`: 修改RDS实例参数
* `restart_db_instance`: 重启RDS实例
### SQL 工具
> MCP 服务器将自动创建一个只读账户，执行 SQL 语句，然后自动删除该账户。此过程要求 MCP 服务器能够连接到数据库实例。

* `explain_sql`：对单条 `SELECT` 语句执行 SQL `EXPLAIN` 命令并返回 SQL 执行计划结果。
* `show_engine_innodb_status`：执行 SQL `show engine innodb status` 命令并返回 SQL 执行结果。
* `show_create_table`：对已校验的数据库名和表名执行 SQL `SHOW CREATE TABLE` 命令并返回 SQL 执行结果。
* `show_largest_table`: 查询空间占用率最高的前几张表。
* `show_largest_table_fragment`: 查询空间碎片率最高的前几张表。
* `query_sql`：执行单条只读 SQL 语句（`SELECT`、`SHOW`、`DESCRIBE` 或 `EXPLAIN`）并返回 SQL 执行结果。


#### 工具集分组

工具集将可用的 MCP 工具进行分组管理，让你只启用需要的功能。启动服务器时可通过以下方式配置工具集：

- **命令行参数**: `--toolsets` 参数
- **环境变量**: `MCP_TOOLSETS`

#### 格式
使用逗号分隔的工具集名称（逗号周围不要空格）：
```
rds,rds_mssql_custom
```

#### 可用工具集
以下是工具集及其功能的列表：

- **rds**：启用标准托管 RDS 服务的所有工具
- **rds_custom_read**：为 RDS Custom 启用只读工具
- **rds_custom_all**：为 RDS Custom 启用所有工具


#### 示例
```bash
# 单个工具集
--toolsets rds

# 多个工具集
--toolsets rds,rds_mssql_custom

# 环境变量方式
export MCP_TOOLSETS=rds,rds_mssql_custom
```

#### 默认行为
如果未指定工具集，将自动加载默认的 `rds` 工具组。

### 资源
当前暂无资源

### Skills (技能扩展)
本项目支持通过 Skills 扩展功能，增强 RDS Copilot 的能力。当前提供：

- **alibabacloud-rds-copilot**：调用 RDS AI 助手 API，完成智能问答、SQL 优化、故障排查等。
- **alibabacloud-rds-instances-manage**：以**脚本/命令行**形式暴露本项目的 **OpenAPI 工具** 与 **SQL 工具**，支持接入 **OpenClaw、Claude Code** 等：大模型通过执行 `alibabacloud-rds-instances-manage list` 与 `alibabacloud-rds-instances-manage run <工具名> '<JSON 参数>'` 管理 RDS 实例（查询实例/监控/慢日志/参数、执行只读 SQL 等）。安装本包后即可使用 `alibabacloud-rds-instances-manage` 命令；Skill 目录见 `skill/alibabacloud-rds-instances-manage/`。

详细使用说明请参考：[Skills 使用文档](./skill/skill_readme_cn.md)

### 提示模板
```markdown
# 角色  
你是一位专业的阿里云RDS Copilot,专注于为客户提供关于RDS(关系型数据库服务)的高效技术支持和解答。你的目标是通过清晰的问题拆解、精准的工具调用以及准确的时间计算,帮助客户快速解决问题。

## 技能  

### 技能一:问题拆解与分析  
- 能够对用户提出的问题进行深入拆解,明确问题的核心需求及可能涉及的步骤或指令。
- 提供清晰的任务分解步骤,确保每一步都能指向最终解决方案。
- 回答结果中尽量以表格的形式进行整理。

### 技能二:RDS MCP工具调用  
- 熟练调用RDS MCP工具获取数据库相关信息或执行相关操作。
- 工具调用前必须先完成任务拆解,并确保调用逻辑清晰且符合客户需求。
- 根据用户的具体问题,选择合适的MCP功能模块进行操作,如监控数据查询、性能诊断、备份恢复等。

### 技能三:时间理解与计算  
- 能够准确解析用户提出的相对时间概念,例如"今天"、"昨天"、"最近一小时"等。
- 通过获取当前时间,将相对时间转换为具体的时间范围或时间点,以支持后续的数据查询或操作。

## 限制条件  
- **任务拆解优先**:必须先给出详细的任务拆解步骤。
- **工具依赖明确**:所有需要调用RDS MCP工具的操作,都应基于清晰的任务需求和逻辑推理。
- **时间计算精确**:对于涉及时间的查询,必须准确计算出对应的具体时间范围。
- **专业性保障**:仅讨论与阿里云RDS相关的技术问题,避免偏离主题。
- **安全性注意**:在执行任何操作时,需确保不会对客户的数据库造成负面影响。
```

## 贡献指南
欢迎贡献代码！请提交Pull Request：
1. Fork 本仓库
2. 创建特性分支（`git checkout -b feature/amazing-feature`）
3. 提交修改（`git commit -m '添加新特性'`）
4. 推送分支（`git push origin feature/amazing-feature`）
5. 发起Pull Request

## 许可证
本项目采用Apache 2.0许可证

## 联系信息
如有任何疑问或疑虑，请通过钉钉群联系我们：106730017609

<img src="./assets/dingding.png" alt="store" width="500"/>
