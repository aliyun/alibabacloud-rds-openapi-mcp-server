import json
import logging
import os
import sys
import argparse
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)
import time
from datetime import datetime
from typing import Dict, Any, List, Optional

from alibabacloud_bssopenapi20171214 import models as bss_open_api_20171214_models
from alibabacloud_openapi_util.client import Client as OpenApiUtilClient
from alibabacloud_rds20140815 import models as rds_20140815_models
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models
from alibabacloud_vpc20160428 import models as vpc_20160428_models

current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

src_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if src_path not in sys.path:
    sys.path.insert(0, src_path)

from db_service import DBService
from utils import (transform_to_iso_8601,
                   transform_to_datetime,
                   transform_perf_key,
                   json_array_to_csv,
                   get_rds_client,
                   get_vpc_client,
                   get_bill_client, get_das_client, convert_datetime_to_timestamp)
from alibabacloud_rds_openapi_mcp_server.core.mcp import RdsMCP
DEFAULT_TOOL_GROUP = 'rds'

logger = logging.getLogger(__name__)
mcp = RdsMCP("Alibaba Cloud RDS OPENAPI", port=os.getenv("SERVER_PORT", 8000))
try:
    import alibabacloud_rds_openapi_mcp_server.tools
    import alibabacloud_rds_openapi_mcp_server.prompts
except Exception as e:
    print(f"ERROR: Failed to import component packages: {e}")

class OpenAPIError(Exception):
    """Custom exception for RDS OpenAPI related errors."""
    pass


@mcp.tool()
async def show_engine_innodb_status(
        dbinstance_id: str,
        region_id: str
) -> str:
    """
    show engine innodb status in db.
    Args:
        dbinstance_id (str): The ID of the RDS instance.
        region_id(str): the region id of instance.
    Returns:
        the sql result.
    """
    try:
        with DBService(region_id, dbinstance_id) as service:
            return service.execute_sql("show engine innodb status")
    except Exception as e:
        logger.error(f"Error occurred: {str(e)}")
        raise e

@mcp.tool()
async def show_create_table(
        region_id: str,
        dbinstance_id: str,
        db_name: str,
        table_name: str
) -> str:
    """
    show create table db_name.table_name
    Args:
        dbinstance_id (str): The ID of the RDS instance.
        region_id(str): the region id of instance.
        db_name(str): the db name for table.
        table_name(str): the table name.
    Returns:
        the sql result.
    """
    try:
        with DBService(region_id, dbinstance_id, db_name) as service:
            return service.execute_sql(f"show create table {db_name}.{table_name}")
    except Exception as e:
        logger.error(f"Error occurred: {str(e)}")
        raise e

        
def main(toolsets: Optional[str] = None) -> None:
    """
    Initializes, activates, and runs the MCP server engine.

    This function serves as the main entry point for the application. It
    orchestrates the entire server lifecycle: determining which component
    groups to activate based on a clear precedence order, activating them,
    and finally starting the server's transport layer.

    The component groups to be loaded are determined with the following priority:
      1. --toolsets command-line argument.
      2. MCP_TOOLSETS environment variable.
      3. A default group ('rds') if neither of the above is provided.

    Args:
        toolsets: A comma-separated string of group names passed from
                      the command line.
    """
    source_string = toolsets or os.getenv("MCP_TOOLSETS")

    enabled_groups = _parse_groups_from_source(source_string)

    mcp.activate(enabled_groups=enabled_groups)

    transport = os.getenv("SERVER_TRANSPORT", "stdio")
    mcp.run(transport=transport)


def _parse_groups_from_source(source: str | None) -> List[str]:
    if not source:
        return [DEFAULT_TOOL_GROUP]
    groups = [g.strip() for g in source.split(",") if g.strip()]
    return groups or [DEFAULT_TOOL_GROUP]

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--toolsets",
        help="Comma-separated list of toolset groups to enable (e.g., 'rds,rds_custom')."
    )
    args = parser.parse_args()
    main(toolsets=args.toolsets)

