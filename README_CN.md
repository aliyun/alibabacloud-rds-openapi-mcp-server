<p align="center"><a href="./README.md">English</a> | 中文<br></p>

# 阿里云RDS OpenAPI MCP 服务
RDS OpenAPI MCP服务。
## 前提条件
1. 从[Astral](https://docs.astral.sh/uv/getting-started/installation/)或[GitHub README](https://github.com/astral-sh/uv#installation)安装`uv`
2. 使用`uv python install 3.12`安装Python
3. 具有阿里云RDS服务访问权限的账号凭证

## 快速开始
### 使用[cherry-studio](https://github.com/CherryHQ/cherry-studio)（推荐）
根据[Cherry-Studio文档](https://docs.cherry-ai.com/advanced-basic/mcp/install)安装MCP环境后配置使用RDS MCP。 MCP配置文件格式如下：
```json
"mcpServers": {
  "rds-openapi-mcp-server": {
    "command": "uvx",
    "args": [
      "alibabacloud-rds-openapi-mcp-server@latest"
    ],
    "env": {
      "ALIBABA_CLOUD_ACCESS_KEY_ID": "access_id",
      "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "access_key"
    }
  }
}
```

### 使用Cline
设置环境变量并运行MCP服务
```shell
# 设置环境变量
export SERVER_TRANSPORT=sse;
export ALIBABA_CLOUD_ACCESS_KEY_ID=$your_access_id;  # 替换为你的access_id
export ALIBABA_CLOUD_ACCESS_KEY_SECRET=$your_access_key;  # 替换为你的access_key

# 启动MCP服务
uvx alibabacloud-rds-openapi-mcp-server@latest
```
成功启动后会看到以下输出：
```shell
INFO:     Started server process [91594]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```
然后在Cline中配置：
```shell
remote_server = "http://127.0.0.1:8000/sse";
```

### 使用Claude
从Github克隆仓库
```shell
git clone https://github.com/aliyun/alibabacloud-rds-openapi-mcp-server.git
```
在MCP客户端配置文件中添加：
```json
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
      "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "access_key"
    }
  }
}
```

## 功能组件
### 工具集
* `create_db_instance`: 创建RDS实例
* `describe_db_instances`: 查询实例列表
* `describe_db_instance_attribute`: 查询实例详情
* `describe_db_instance_metrics`: 查询实例性能数据、错误日志和SQL报告
* `describe_db_instance_performance`: 查询实例性能数据
* `describe_error_logs`: 查询实例错误日志
* `describe_db_instance_net_info`: 批量查询实例网络信息
* `describe_db_instance_ip_allowlist`: 批量查询实例IP白名单配置
* `describe_db_instance_databases`: 批量查询实例的DB信息
* `describe_db_instance_accounts`: 批量查询实例的账号信息
* `describe_available_classes`: 查询可用实例规格和存储范围
* `describe_available_zones`: 查询RDS可用区
* `describe_bills`: 批量查询实例账单信息.
* `describe_vpcs`: 查询VPC列表
* `describe_vswitches`: 查询虚拟交换机列表
* `describe_slow_log_records`: 查询RDS慢日志记录
* `modify_parameter`: 修改RDS实例参数
* `modify_db_instance_spec`: 调整RDS实例规格
* `get_current_time`: 获取当前时间

### 资源
当前暂无资源

### 提示模板
```markdown
# 角色  
你是一位专业的阿里云RDS Copilot，专注于为客户提供关于RDS（关系型数据库服务）的高效技术支持和解答。你的目标是通过清晰的问题拆解、精准的工具调用以及准确的时间计算，帮助客户快速解决问题。

## 技能  

### 技能一：问题拆解与分析  
- 能够对用户提出的问题进行深入拆解，明确问题的核心需求及可能涉及的步骤或指令。  
- 提供清晰的任务分解步骤，确保每一步都能指向最终解决方案。  

### 技能二：RDS MCP工具调用  
- 熟练调用RDS MCP工具获取数据库相关信息或执行相关操作。  
- 工具调用前必须先完成任务拆解，并确保调用逻辑清晰且符合客户需求。  
- 根据用户的具体问题，选择合适的MCP功能模块进行操作，如监控数据查询、性能诊断、备份恢复等。  

### 技能三：时间理解与计算  
- 能够准确解析用户提出的相对时间概念，例如“今天”、“昨天”、“最近一小时”等。  
- 通过获取当前时间，将相对时间转换为具体的时间范围或时间点，以支持后续的数据查询或操作。  

## 限制条件  
- **任务拆解优先**：必须先给出详细的任务拆解步骤。  
- **工具依赖明确**：所有需要调用RDS MCP工具的操作，都应基于清晰的任务需求和逻辑推理。  
- **时间计算精确**：对于涉及时间的查询，必须准确计算出对应的具体时间范围。  
- **专业性保障**：仅讨论与阿里云RDS相关的技术问题，避免偏离主题。  
- **安全性注意**：在执行任何操作时，需确保不会对客户的数据库造成负面影响。
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

