<p align="center">English | <a href="./README_CN.md">中文</a><br></p>

# Alibaba Cloud RDS OpenAPI MCP Server
MCP server for RDS Services via OPENAPI

## Prerequisites
1. Install `uv` from [Astral](https://docs.astral.sh/uv/getting-started/installation/) or the [GitHub README](https://github.com/astral-sh/uv#installation)
2. Install Python using `uv python install 3.12`
3. Alibaba Cloud credentials with access to Alibaba Cloud RDS services

## Quick Start
### Using [cherry-studio](https://github.com/CherryHQ/cherry-studio) (Recommended)
1. Download and install cherry-studio
2. Follow the [documentation](https://docs.cherry-ai.com/cherry-studio/download) to install uv, which is required for the MCP environment
3. Configure and use RDS MCP according to the [documentation](https://docs.cherry-ai.com/advanced-basic/mcp/install). You can quickly import the RDS MCP configuration using the JSON below. Please set ALIBABA_CLOUD_ACCESS_KEY_ID and ALIBABA_CLOUD_ACCESS_KEY_SECRET to your Alibaba Cloud AK/SK.

> The following error may appear during import, which can be ignored:
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

4. Finally, click to turn on MCP
<img src="./assets/mcp_turn_on.png" alt="mcp_turn_on"/>

5. You can use the prompt template provided below to enhance your experience.

### Using Cline
Set you env and run mcp server.
```shell
# set env
export SERVER_TRANSPORT=sse;
export ALIBABA_CLOUD_ACCESS_KEY_ID=$you_access_id;
export ALIBABA_CLOUD_ACCESS_KEY_SECRET=$you_access_key;
export ALIBABA_CLOUD_SECURITY_TOKEN=$you_sts_security_token; # optional, required when using STS Token 
export API_KEY=$you_mcp_server_api_key; # Optional, after configuration, requests will undergo API Key authentication.

# run mcp server
uvx alibabacloud-rds-openapi-mcp-server@latest
```
After run mcp server, you will see the following output:
```shell
INFO:     Started server process [91594]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```
And then configure the Cline.
```shell
remote_server = "http://127.0.0.1:8000/sse";
```

> If you encounter a `401 Incorrect API key provided` error when using Qwen, please refer to the [documentation](https://help.aliyun.com/zh/model-studio/cline) for solutions.

### Using Claude
Download from Github
```shell
git clone https://github.com/aliyun/alibabacloud-rds-openapi-mcp-server.git
```
Add the following configuration to the MCP client configuration file:
```json5
{
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
        "ALIBABA_CLOUD_SECURITY_TOKEN": "sts_security_token",
        // optional, required when using STS Token
      }
    }
  }
}
```

## Components
### OpenAPI Tools
* `add_tags_to_db_instance`: Add tags to an RDS instance.
* `allocate_instance_public_connection`: Allocate a public connection for an RDS instance.
* `attach_whitelist_template_to_instance`: Attach a whitelist template to an RDS instance.
* `create_db_instance`: Create an RDS instance.
* `create_db_instance_account`: Create an account for RDS instance.
* `describe_all_whitelist_template`: Query the whitelist template list.
* `describe_available_classes`: Query available instance classes and storage ranges.
* `describe_available_zones`: Query available zones for RDS instances.
* `describe_bills`: Query the consumption summary of all product instances or billing items for a user within a specific billing period.
* `describe_db_instance_accounts`: Batch retrieves account information for multiple RDS instances.
* `describe_db_instance_attribute`: Queries the details of an instance.
* `describe_db_instance_databases`: Batch retrieves database information for multiple RDS instances.
* `describe_db_instance_ip_allowlist`: Batch retrieves IP allowlist configurations for multiple RDS instances.
* `describe_db_instance_net_info`: Batch retrieves network configuration details for multiple RDS instances.
* `describe_db_instance_parameters`: Batch retrieves parameter information for multiple RDS instances.
* `describe_db_instance_performance`: Queries the performance data of an instance.
* `describe_db_instances`: Queries instances. Support passing SQL for data analysis and statistics
* `describe_error_logs`: Queries the error log of an instance.
* `describe_instance_linked_whitelist_template`: Query the whitelist template list.
* `describe_monitor_metrics`: Queries performance and diagnostic metrics for an instance using the DAS (Database Autonomy Service) API.
* `describe_slow_log_records`: Query slow log records for an RDS instance.
* `describe_sql_insight_statistic`: Query SQL Log statistics, including SQL cost time, execution times, and account.
* `describe_vpcs`: Query VPC list.
* `describe_vswitches`: Query VSwitch list.
* `modify_security_ips`: Modify RDS instance security IP whitelist.
* `get_current_time`: Get the current time.
* `modify_db_instance_description`: Modify RDS instance descriptions.
* `modify_db_instance_spec`: Modify RDS instance specifications.
* `modify_parameter`: Modify RDS instance parameters.
* `restart_db_instance`: Restart an RDS instance.
### SQL Tools
> The MCP Server will automatically create a read-only account, execute the SQL statement, and then automatically delete the account. This process requires that the MCP Server can connect to the instance.

* `explain_sql`: Execute sql `explain` and return sql result.
* `show_engine_innodb_status`: Execute sql `show engine innodb status` and return sql result.
* `show_create_table`: Execute sql `show create table` and return sql result.
* `show_largest_table`: Query the top few tables with the highest space occupancy.
* `show_largest_table_fragment`: Query the tables with the largest table fragments.
* `query_sql`: Execute read-only sql and return sql result.

### Toolsets

Toolsets group available MCP tools so you can enable only what you need. Configure toolsets when starting the server using either:

- **Command line**: `--toolsets` parameter
- **Environment variable**: `MCP_TOOLSETS`

#### Available Toolsets

Here is a list of toolsets and their functions:

- **rds**: Enables all tools for the standard, managed RDS service

- **rds_custom_read**: Enables read-only tools for the RDS Custom. 

- **rds_custom_all**: Enables full read and write tools for the RDS Custom.

#### Format
Use comma-separated toolset names (no spaces around commas):
```
rds,rds_custom_all
```

#### Examples
```bash
# Single toolset
--toolsets rds

# Multiple tools
--toolsets rds,rds_mssql_custom

# Environment variable
export MCP_TOOLSETS=rds,rds_custom_all
```

#### Default Behavior
If no toolset is specified, the default `rds` group is loaded automatically.

### Resources
None at this time

### Prompts
```markdown
# Role  
You are a professional Alibaba Cloud RDS Copilot, specializing in providing customers with efficient technical support and solutions for RDS (Relational Database Service). Your goal is to help customers resolve issues quickly through clear problem decomposition, precise tool invocation, and accurate time calculations.

## Skills  

### Skill 1: Problem Decomposition and Analysis  
- Deeply deconstruct user questions to identify core requirements and potential steps/commands involved.  
- Provide clear task breakdowns to ensure each step contributes to the final solution.
- Please organize your answers in a table format as much as possible.

### Skill 2: RDS MCP Tool Invocation  
- Proficiently invoke the RDS MCP tool to retrieve database information or execute operations.  
- Tool invocation must follow task decomposition and align with logical reasoning and customer needs.  
- Select appropriate MCP modules (e.g., monitoring data queries, performance diagnostics, backup/recovery) based on user requirements.  

### Skill 3: Time Interpretation and Calculation  
- Accurately parse relative time concepts like "today," "yesterday," or "the last hour."  
- Convert relative time expressions into precise time ranges or timestamps using the current time to support data queries or operations.  

## Constraints  
- **Task Decomposition First**: Always provide detailed task breakdowns.  
- **Tool Dependency Clarity**: All MCP tool invocations must be justified by clear task requirements and logical reasoning.  
- **Time Precision**: Calculate exact time ranges for time-sensitive queries.  
- **Professional Focus**: Discuss only Alibaba Cloud RDS-related technical topics.  
- **Safety Awareness**: Ensure no operations negatively impact customer databases.
```

## Use Cases
### mydba
Alibaba Cloud Database MyDBA Agent(<a href="./component/mydba/README.md">README.md</a>)
- Buy RDS  
<img src="./assets/buy_rds.gif" alt="buy RDS" width="500"/>
- Diagnose RDS  
<img src="./assets/diagnose.gif" alt="diagnose RDS" width="500"/>

## Contributing
Contributions are welcome! Please feel free to submit a Pull Request.
1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License
This project is licensed under the Apache 2.0 License.

## Contact Information
For any questions or concerns, please contact us through the DingTalk group：106730017609

<img src="./assets/dingding.png" alt="store" width="500"/>