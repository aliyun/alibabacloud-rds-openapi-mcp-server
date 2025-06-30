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
async def describe_db_instances(region_id: str):
    """
    Queries instances.
    Args:
        region_id: queries instances in region id(e.g. cn-hangzhou)
    :return:
    """
    client = get_rds_client(region_id)
    try:
        request = rds_20140815_models.DescribeDBInstancesRequest(
            region_id=region_id,
            page_size=100
        )
        response = client.describe_dbinstances(request)
        
        res = json_array_to_csv(response.body.items.dbinstance)
        if not res:
            return "No RDS instances found."
        return res
    except Exception as e:
        raise e


@mcp.tool()
async def describe_db_instance_attribute(region_id: str, db_instance_id: str):
    """
    Queries the details of an instance.
    Args:
        region_id: db instance region(e.g. cn-hangzhou)
        db_instance_id: db instance id(e.g. rm-xxx)
    :return:
    """
    client = get_rds_client(region_id)
    try:
        request = rds_20140815_models.DescribeDBInstanceAttributeRequest(dbinstance_id=db_instance_id)
        response = client.describe_dbinstance_attribute(request)
        return response.body.to_map()
    except Exception as e:
        raise e


@mcp.tool()
async def describe_db_instance_performance(region_id: str,
                                           db_instance_id: str,
                                           db_type: str,
                                           perf_keys: list[str],
                                           start_time: str,
                                           end_time: str):
    """
    Queries the performance data of an instance.
    Args:
        region_id: db instance region(e.g. cn-hangzhou)
        db_instance_id: db instance id(e.g. rm-xxx)
        db_type: the db instance database type(e.g. mysql,pgsql,sqlserver)
        perf_keys: Performance Key  (e.g. ["MemCpuUsage", "QPSTPS", "Sessions", "COMDML", "RowDML", "ThreadStatus", "MBPS", "DetailedSpaceUsage"])
        start_time: start time(e.g. 2023-01-01 00:00)
        end_time: end time(e.g. 2023-01-01 00:00)
    """

    def _compress_performance(performance_value, max_items=10):
        if len(performance_value) > max_items:
            result = []
            offset = len(performance_value) / 10
            for i in range(0, len(performance_value), int(offset)):
                _item = None
                for j in range(i, min(i + int(offset), len(performance_value))):
                    if _item is None or sum([float(v) for v in performance_value[j].value.split('&')]) > sum(
                            [float(v) for v in _item.value.split('&')]):
                        _item = performance_value[j]
                else:
                    result.append(_item)
            return result
        else:
            return performance_value

    try:
        start_time = transform_to_datetime(start_time)
        end_time = transform_to_datetime(end_time)
        client = get_rds_client(region_id)
        perf_key = transform_perf_key(db_type, perf_keys)
        if not perf_key:
            raise OpenAPIError(f"Unsupported perf_key: {perf_key}")
        request = rds_20140815_models.DescribeDBInstancePerformanceRequest(
            dbinstance_id=db_instance_id,
            start_time=transform_to_iso_8601(start_time, "minutes"),
            end_time=transform_to_iso_8601(end_time, "minutes"),
            key=",".join(perf_key)
        )
        response = client.describe_dbinstance_performance(request)
        responses = []
        for perf_key in response.body.performance_keys.performance_key:
            perf_key_info = f"""Key={perf_key.key}; Unit={perf_key.unit}; ValueFormat={perf_key.value_format}; Values={json_array_to_csv(_compress_performance(perf_key.values.performance_value))}"""
            responses.append(perf_key_info)
        return responses
    except Exception as e:
        raise e


@mcp.tool()
async def modify_parameter(
        region_id: str,
        dbinstance_id: str,
        parameters: Dict[str, str] = None,
        parameter_group_id: str = None,
        forcerestart: bool = False,
        switch_time_mode: str = "Immediate",
        switch_time: str = None,
        client_token: str = None
) -> Dict[str, Any]:
    """Modify RDS instance parameters.

    Args:
        region_id: The region ID of the RDS instance.
        dbinstance_id: The ID of the RDS instance.
        parameters (Dict[str, str], optional): Parameters and their values in JSON format.
            Example: {"delayed_insert_timeout": "600", "max_length_for_sort_data": "2048"}
        parameter_group_id: Parameter template ID.
        forcerestart: Whether to force restart the database. Default: False.
        switch_time_mode: Execution time mode. Values: Immediate, MaintainTime, ScheduleTime. Default: Immediate.
        switch_time: Scheduled execution time in format: yyyy-MM-ddTHH:mm:ssZ (UTC time).
        client_token: Client token for idempotency, max 64 ASCII characters.

    Returns:
        Dict[str, Any]: The response containing the request ID.
    """
    try:
        # Initialize the client
        client = get_rds_client(region_id)

        # Create request
        request = rds_20140815_models.ModifyParameterRequest(
            dbinstance_id=dbinstance_id,
            forcerestart=forcerestart,
            switch_time_mode=switch_time_mode
        )

        # Add optional parameters if provided
        if parameters:
            request.parameters = json.dumps(parameters)
        if parameter_group_id:
            request.parameter_group_id = parameter_group_id
        if switch_time:
            request.switch_time = switch_time
        if client_token:
            request.client_token = client_token

        # Make the API request
        response = client.modify_parameter(request)
        return response.body.to_map()

    except Exception as e:
        logger.error(f"Error occurred while modifying parameters: {str(e)}")
        raise OpenAPIError(f"Failed to modify RDS instance parameters: {str(e)}")


@mcp.tool()
async def modify_db_instance_spec(
        region_id: str,
        dbinstance_id: str,
        dbinstance_class: str = None,
        dbinstance_storage: int = None,
        pay_type: str = None,
        effective_time: str = None,
        switch_time: str = None,
        switch_time_mode: str = None,
        source_biz: str = None,
        dedicated_host_group_id: str = None,
        zone_id: str = None,
        vswitch_id: str = None,
        category: str = None,
        instance_network_type: str = None,
        direction: str = None,
        auto_pause: bool = None,
        max_capacity: float = None,
        min_capacity: float = None,
        switch_force: bool = None,
        client_token: str = None
) -> Dict[str, Any]:
    """Modify RDS instance specifications.

    Args:
        region_id: The region ID of the RDS instance.
        dbinstance_id: The ID of the RDS instance.
        dbinstance_class: Target instance specification.
        dbinstance_storage: Target storage space in GB.
        pay_type: Instance payment type. Values: Postpaid, Prepaid, Serverless.
        effective_time: When the new configuration takes effect. Values: Immediate, MaintainTime, ScheduleTime.
        switch_time: Scheduled switch time in format: yyyy-MM-ddTHH:mm:ssZ (UTC time).
        switch_time_mode: Switch time mode. Values: Immediate, MaintainTime, ScheduleTime.
        source_biz: Source business type.
        dedicated_host_group_id: Dedicated host group ID.
        zone_id: Zone ID.
        vswitch_id: VSwitch ID.
        category: Instance category.
        instance_network_type: Instance network type.
        direction: Specification change direction. Values: UP, DOWN.
        auto_pause: Whether to enable auto pause for Serverless instances.
        max_capacity: Maximum capacity for Serverless instances.
        min_capacity: Minimum capacity for Serverless instances.
        switch_force: Whether to force switch for Serverless instances.
        client_token: Client token for idempotency, max 64 ASCII characters.

    Returns:
        Dict[str, Any]: The response containing the request ID.
    """
    try:
        # Initialize the client
        client = get_rds_client(region_id)

        # Create request
        request = rds_20140815_models.ModifyDBInstanceSpecRequest(
            dbinstance_id=dbinstance_id
        )

        # Add optional parameters if provided
        if dbinstance_class:
            request.dbinstance_class = dbinstance_class
        if dbinstance_storage:
            request.dbinstance_storage = dbinstance_storage
        if pay_type:
            request.pay_type = pay_type
        if effective_time:
            request.effective_time = effective_time
        if switch_time:
            request.switch_time = switch_time
        if switch_time_mode:
            request.switch_time_mode = switch_time_mode
        if source_biz:
            request.source_biz = source_biz
        if dedicated_host_group_id:
            request.dedicated_host_group_id = dedicated_host_group_id
        if zone_id:
            request.zone_id = zone_id
        if vswitch_id:
            request.vswitch_id = vswitch_id
        if category:
            request.category = category
        if instance_network_type:
            request.instance_network_type = instance_network_type
        if direction:
            request.direction = direction
        if auto_pause is not None:
            request.auto_pause = auto_pause
        if max_capacity:
            request.max_capacity = max_capacity
        if min_capacity:
            request.min_capacity = min_capacity
        if switch_force is not None:
            request.switch_force = switch_force
        if client_token:
            request.client_token = client_token

        # Make the API request
        response = client.modify_dbinstance_spec(request)
        return response.body.to_map()

    except Exception as e:
        logger.error(f"Error occurred while modifying instance specifications: {str(e)}")
        raise OpenAPIError(f"Failed to modify RDS instance specifications: {str(e)}")


@mcp.tool()
async def describe_available_classes(
        region_id: str,
        zone_id: str,
        instance_charge_type: str,
        engine: str,
        engine_version: str,
        dbinstance_storage_type: str,
        category: str,
        dbinstance_id: str = None,
        order_type: str = None,
        commodity_code: str = None
) -> Dict[str, Any]:
    """Query the RDS instance class_code and storage space that can be purchased in the inventory.

    Args:
        region_id: The region ID of the RDS instance.
        zone_id: The zone ID of the RDS instance. Query available zones by `describe_available_zones`.
        instance_charge_type: Instance payment type. Values: Prepaid, Postpaid, Serverless.
        engine: Database engine type. Values: MySQL, SQLServer, PostgreSQL, MariaDB.
        engine_version: Database version.
        dbinstance_storage_type: Storage type. Values: local_ssd,general_essd,cloud_essd,cloud_essd2,cloud_essd3
        category: Instance category. Values: Basic, HighAvailability, cluster, AlwaysOn, Finance, serverless_basic, serverless_standard, serverless_ha.
        dbinstance_id: The ID of the RDS instance.
        order_type: Order type. Currently only supports "BUY".
        commodity_code: Commodity code for read-only instances.

    Returns:
        Dict[str, Any]: The response containing available instance classes and storage ranges.
    """
    try:
        # Initialize the client
        client = get_rds_client(region_id)

        # Create request
        request = rds_20140815_models.DescribeAvailableClassesRequest(
            region_id=region_id,
            zone_id=zone_id,
            instance_charge_type=instance_charge_type,
            engine=engine,
            engine_version=engine_version,
            dbinstance_storage_type=dbinstance_storage_type,
            category=category
        )

        # Add optional parameters if provided
        if dbinstance_id:
            request.dbinstance_id = dbinstance_id
        if order_type:
            request.order_type = order_type
        if commodity_code:
            request.commodity_code = commodity_code

        # Make the API request
        response = client.describe_available_classes(request)
        return response.body.to_map()

    except Exception as e:
        logger.error(f"Error occurred while querying available classes: {str(e)}")
        raise OpenAPIError(f"Failed to query available instance classes: {str(e)}")


@mcp.tool()
async def create_db_instance(
        region_id: str,
        engine: str,
        engine_version: str,
        dbinstance_class: str,
        dbinstance_storage: int,
        vpc_id: str,
        vswitch_id: str,
        zone_id: str,
        zone_id_slave1: str = None,
        zone_id_slave2: str = None,
        security_ip_list: str = "127.0.0.1",
        instance_network_type: str = "VPC",
        pay_type: str = "Postpaid",
        system_db_charset: str = None,
        dbinstance_net_type: str = "Internet",
        category: str = "Basic",
        dbinstance_storage_type: str = None,
        private_ip_address: str = None,
        client_token: str = None,
        resource_group_id: str = None,
        tde_status: str = None,
        encryption_key: str = None,
        serverless_config: Dict[str, Any] = None,
        table_names_case_sensitive: bool = False,
        db_time_zone: str = None,
        connection_string: str = None,
        db_param_group_id: str = None,
) -> Dict[str, Any]:
    """Create an RDS instance.

    Args:
        region_id: Region ID.
        engine: Database type (MySQL, SQLServer, PostgreSQL, MariaDB).
        engine_version: Database version.
        dbinstance_class: Instance specification. Query available class_codes by `describe_available_classes`.
        dbinstance_storage: Storage space in GB.
        security_ip_list: IP whitelist, separated by commas. Default: "127.0.0.1".
        instance_network_type: Network type (Classic, VPC). Default: VPC.
        zone_id: Zone ID. Query available zones by `describe_available_zones`.
        zone_id_slave1: Slave Node1 Zone ID. Query available zones by `describe_available_zones`.
        zone_id_slave2: Slave Node2 Zone ID. Query available zones by `describe_available_zones`.
        pay_type: Payment type (Postpaid, Prepaid). Default: Postpaid.
        instance_charge_type: Instance charge type.
        system_db_charset: Character set.
        dbinstance_net_type: Network connection type (Internet, Intranet). Default: Internet.
        category: Instance category. Default: Basic.
        dbinstance_storage_type: Storage type. (e.g. local_ssd,general_essd,cloud_essd,cloud_essd2,cloud_essd3)
        vpc_id: VPC ID.
        vswitch_id: VSwitch ID.
        private_ip_address: Private IP address.
        client_token: Idempotence token.
        resource_group_id: Resource group ID.
        tde_status: TDE status (Enable, Disable).
        encryption_key: Custom encryption key.
        serverless_config: Serverless instance configuration.
        table_names_case_sensitive: Are table names case-sensitive.
        db_time_zone: the db instance time zone.
        connection_string: the connection string for db instance.
        db_param_group_id: the db param group id for db instance.
    Returns:
        Dict[str, Any]: Response containing the created instance details.
    """
    try:
        client = get_rds_client(region_id)

        request = rds_20140815_models.CreateDBInstanceRequest(
            region_id=region_id,
            engine=engine,
            engine_version=engine_version,
            dbinstance_class=dbinstance_class,
            dbinstance_storage=dbinstance_storage,
            security_iplist=security_ip_list,
            instance_network_type=instance_network_type,
            dbis_ignore_case=str(not table_names_case_sensitive).lower(),
            dbtime_zone=db_time_zone,
            connection_string=connection_string,
            dbparam_group_id=db_param_group_id
        )

        # Add optional parameters
        if zone_id:
            request.zone_id = zone_id
        if zone_id_slave1:
            request.zone_id_slave_1 = zone_id_slave1
        if zone_id_slave2:
            request.zone_id_slave_2 = zone_id_slave2
        if pay_type:
            request.pay_type = pay_type
        if system_db_charset:
            request.system_dbcharset = system_db_charset
        if dbinstance_net_type:
            request.dbinstance_net_type = dbinstance_net_type
        if category:
            request.category = category
        if dbinstance_storage_type:
            request.dbinstance_storage_type = dbinstance_storage_type
        if vpc_id:
            request.vpcid = vpc_id
        if vswitch_id:
            request.v_switch_id = vswitch_id
        if private_ip_address:
            request.private_ip_address = private_ip_address
        if client_token:
            request.client_token = client_token
        if resource_group_id:
            request.resource_group_id = resource_group_id
        if tde_status:
            request.tde_status = tde_status
        if encryption_key:
            request.encryption_key = encryption_key
        if serverless_config:
            request.serverless_config = json.dumps(serverless_config)

        response = client.create_dbinstance(request)
        return response.body.to_map()

    except Exception as e:
        logger.error(f"Error occurred while creating RDS instance: {str(e)}")
        raise e


@mcp.tool()
async def describe_available_zones(
        region_id: str,
        engine: str,
        engine_version: str = None,
        commodity_code: str = None,
        zone_id: str = None,
        dispense_mode: str = None,
        dbinstance_name: str = None,
        category: str = None
) -> Dict[str, Any]:
    """Query available zones for RDS instances.

    Args:
        region_id: Region ID.
        engine: Database type (MySQL, SQLServer, PostgreSQL, MariaDB).
        engine_version: Database version.
            MySQL: 5.5, 5.6, 5.7, 8.0
            SQL Server: 2008r2, 2012, 2014, 2016, 2017, 2019
            PostgreSQL: 10.0, 11.0, 12.0, 13.0, 14.0, 15.0
            MariaDB: 10.3
        commodity_code: Commodity code.
            bards: Pay-as-you-go primary instance (China site)
            rds: Subscription primary instance (China site)
            rords: Pay-as-you-go read-only instance (China site)
            rds_rordspre_public_cn: Subscription read-only instance (China site)
            bards_intl: Pay-as-you-go primary instance (International site)
            rds_intl: Subscription primary instance (International site)
            rords_intl: Pay-as-you-go read-only instance (International site)
            rds_rordspre_public_intl: Subscription read-only instance (International site)
            rds_serverless_public_cn: Serverless instance (China site)
            rds_serverless_public_intl: Serverless instance (International site)
        zone_id: Zone ID.
        dispense_mode: Whether to return zones that support single-zone deployment.
            1: Return (default)
            0: Do not return
        dbinstance_name: Primary instance ID. Required when querying read-only instance resources.
        category: Instance category.
            Basic: Basic Edition
            HighAvailability: High-availability Edition
            cluster: MySQL Cluster Edition
            AlwaysOn: SQL Server Cluster Edition
            Finance: Enterprise Edition
            serverless_basic: Serverless Basic Edition
            serverless_standard: MySQL Serverless High-availability Edition
            serverless_ha: SQL Server Serverless High-availability Edition

    Returns:
        Dict[str, Any]: Response containing available zones information.
    """
    try:
        # Initialize the client
        client = get_rds_client(region_id)

        # Create request
        request = rds_20140815_models.DescribeAvailableZonesRequest(
            region_id=region_id,
            engine=engine
        )

        # Add optional parameters if provided
        if engine_version:
            request.engine_version = engine_version
        if commodity_code:
            request.commodity_code = commodity_code
        if zone_id:
            request.zone_id = zone_id
        if dispense_mode:
            request.dispense_mode = dispense_mode
        if dbinstance_name:
            request.dbinstance_name = dbinstance_name
        if category:
            request.category = category

        # Make the API request
        response = client.describe_available_zones(request)
        return response.body.to_map()

    except Exception as e:
        logger.error(f"Error occurred while querying available zones: {str(e)}")
        raise OpenAPIError(f"Failed to query available zones: {str(e)}")


@mcp.tool()
async def describe_vpcs(
        region_id: str,
        vpc_id: str = None,
        vpc_name: str = None,
        resource_group_id: str = None,
        page_number: int = 1,
        page_size: int = 10,
        vpc_owner_id: int = None,
        tags: List[Dict[str, str]] = None
) -> Dict[str, Any]:
    """Query VPC list.

    Args:
        region_id: The region ID of the VPC.
        vpc_id: The ID of the VPC. Up to 20 VPC IDs can be specified, separated by commas.
        vpc_name: The name of the VPC.
        resource_group_id: The resource group ID of the VPC to query.
        page_number: The page number of the list. Default: 1.
        page_size: The number of entries per page. Maximum value: 50. Default: 10.
        vpc_owner_id: The Alibaba Cloud account ID of the VPC owner.
        tags: The tags of the resource.

    Returns:
        Dict[str, Any]: The response containing the list of VPCs.
    """
    try:
        # Initialize the client
        client = get_vpc_client(region_id)

        # Create request
        request = vpc_20160428_models.DescribeVpcsRequest(
            region_id=region_id,
            page_number=page_number,
            page_size=page_size
        )

        # Add optional parameters if provided
        if vpc_id:
            request.vpc_id = vpc_id
        if vpc_name:
            request.vpc_name = vpc_name
        if resource_group_id:
            request.resource_group_id = resource_group_id
        if vpc_owner_id:
            request.vpc_owner_id = vpc_owner_id
        if tags:
            request.tag = tags

        # Make the API request
        response = client.describe_vpcs(request)
        return response.to_map()

    except Exception as e:
        logger.error(f"Error occurred while querying VPCs: {str(e)}")
        raise OpenAPIError(f"Failed to query VPCs: {str(e)}")


@mcp.tool()
async def describe_vswitches(
        region_id: str = None,
        vpc_id: str = None,
        vswitch_id: str = None,
        zone_id: str = None,
        vswitch_name: str = None,
        is_default: bool = None,
        resource_group_id: str = None,
        page_number: int = 1,
        page_size: int = 10,
) -> Dict[str, Any]:
    """Query VSwitch list.

    Args:
        region_id: The region ID of the VSwitch. At least one of region_id or vpc_id must be specified.
        vpc_id: The ID of the VPC to which the VSwitch belongs. At least one of region_id or vpc_id must be specified.
        vswitch_id: The ID of the VSwitch to query.
        zone_id: The zone ID of the VSwitch.
        vswitch_name: The name of the VSwitch.
        resource_group_id: The resource group ID of the VSwitch.
        page_number: The page number of the list. Default: 1.
        page_size: The number of entries per page. Maximum value: 50. Default: 10.

    Returns:
        Dict[str, Any]: The response containing the list of VSwitches.
    """
    try:
        # Initialize the client
        if not region_id and not vpc_id:
            raise OpenAPIError("At least one of region_id or vpc_id must be specified")

        client = get_vpc_client(region_id)

        # Create request
        request = vpc_20160428_models.DescribeVSwitchesRequest(
            page_number=page_number,
            page_size=page_size
        )

        # Add optional parameters if provided
        if vpc_id:
            request.vpc_id = vpc_id
        if vswitch_id:
            request.vswitch_id = vswitch_id
        if zone_id:
            request.zone_id = zone_id
        if vswitch_name:
            request.vswitch_name = vswitch_name
        if is_default is not None:
            request.is_default = is_default
        if resource_group_id:
            request.resource_group_id = resource_group_id

        # Make the API request
        response = client.describe_vswitches(request)
        return response.body.to_map()

    except Exception as e:
        logger.error(f"Error occurred while querying VSwitches: {str(e)}")
        raise OpenAPIError(f"Failed to query VSwitches: {str(e)}")


@mcp.tool()
async def describe_slow_log_records(
        region_id: str,
        dbinstance_id: str,
        start_time: str,
        end_time: str,
        sqlhash: str = None,
        db_name: str = None,
        page_size: int = 30,
        page_number: int = 1,
        node_id: str = None
) -> Dict[str, Any]:
    """Query slow log records for an RDS instance.

    Args:
        region_id: The region ID of the RDS instance.
        dbinstance_id: The ID of the RDS instance.
        start_time: Start time in format: yyyy-MM-dd HH:mm.
            Cannot be earlier than 30 days before the current time.
        end_time: End time in format: yyyy-MM-dd HH:mm.
            Must be later than the start time.
        sqlhash: The unique identifier of the SQL statement in slow log statistics.
            Used to get slow log details for a specific SQL statement.
        db_name: The name of the database.
        page_size: Number of records per page. Range: 30-100. Default: 30.
        page_number: Page number. Must be greater than 0 and not exceed Integer max value. Default: 1.
        node_id: Node ID. Only applicable to cluster instances.
            If not specified, logs from the primary node are returned by default.

    Returns:
        Dict[str, Any]: The response containing slow log records.
    """
    try:
        # Initialize the client
        client = get_rds_client(region_id)
        start_time = transform_to_datetime(start_time)
        end_time = transform_to_datetime(end_time)
        # Create request
        request = rds_20140815_models.DescribeSlowLogRecordsRequest(
            dbinstance_id=dbinstance_id,
            start_time=transform_to_iso_8601(start_time, "minutes"),
            end_time=transform_to_iso_8601(end_time, "minutes"),
            page_size=page_size,
            page_number=page_number
        )

        # Add optional parameters if provided
        if sqlhash:
            request.sqlhash = sqlhash
        if db_name:
            request.db_name = db_name
        if node_id:
            request.node_id = node_id

        # Make the API request
        response = client.describe_slow_log_records(request)
        return response.body.to_map()

    except Exception as e:
        logger.error(f"Error occurred while querying slow log records: {str(e)}")
        raise OpenAPIError(f"Failed to query slow log records: {str(e)}")


@mcp.tool()
async def describe_error_logs(
        region_id: str,
        db_instance_id: str,
        start_time: str,
        end_time: str,
        page_size: int = 30,
        page_number: int = 1
) -> Dict[str, Any]:
    """
    Query error logs of an RDS instance.
    Args:
        region_id: The region ID of the RDS instance.
        db_instance_id: The ID of the RDS instance.
        start_time: The start time of the query. Format: yyyy-MM-dd HH:mm.
        end_time: The end time of the query. Format: yyyy-MM-dd HH:mm.
        page_size: The number of records per page. Range: 30~100. Default: 30.
        page_number: The page number. Default: 1.
    Returns:
        Dict[str, Any]: A dictionary containing error log information
    """
    try:
        start_time = transform_to_datetime(start_time)
        end_time = transform_to_datetime(end_time)
        client = get_rds_client(region_id)
        request = rds_20140815_models.DescribeErrorLogsRequest(
            dbinstance_id=db_instance_id,
            start_time=transform_to_iso_8601(start_time, "minutes"),
            end_time=transform_to_iso_8601(end_time, "minutes"),
            page_size=page_size,
            page_number=page_number
        )
        response = await client.describe_error_logs_async(request)
        return {
            "Logs": "\n".join([log.error_info for log in response.body.items.error_log]),
            "PageNumber": response.body.page_number,
            "PageRecordCount": response.body.page_record_count,
            "TotalRecordCount": response.body.total_record_count
        }
    except Exception as e:
        logger.error(f"Failed to describe error logs: {str(e)}")
        raise OpenAPIError(f"Failed to describe error logs: {str(e)}")


@mcp.tool()
async def describe_db_instance_net_info(
        region_id: str,
        db_instance_ids: list[str]
) -> list[dict]:
    """
    Batch retrieves network configuration details for multiple RDS instances.
    Args:
        region_id: The region ID of the RDS instance.
        db_instance_ids: List of DB instance identifiers (e.g., ["rm-uf6wjk5****", "db-instance-01"])
    Returns:
        list[dict]: A list of dictionaries containing network configuration details for each instance.
    """
    try:
        client = get_rds_client(region_id)
        db_instance_net_infos = []
        for db_instance_id in db_instance_ids:
            request = rds_20140815_models.DescribeDBInstanceNetInfoRequest(
                dbinstance_id=db_instance_id
            )
            response = await client.describe_dbinstance_net_info_async(request)
            db_instance_net_infos.append(response.body.to_map())
        return db_instance_net_infos
    except Exception as e:
        raise e


@mcp.tool()
async def describe_db_instance_ip_allowlist(
        region_id: str,
        db_instance_ids: list[str]
) -> list[dict]:
    """
    Batch retrieves IP allowlist configurations for multiple RDS instances.
    Args:
        region_id: The region ID of the RDS instance.
        db_instance_ids: List of DB instance identifiers (e.g., ["rm-uf6wjk5****", "db-instance-01"])
    Returns:
        list[dict]: A list of dictionaries containing network configuration details for each instance.
    """
    try:
        client = get_rds_client(region_id)
        db_instance_ip_allowlist = []
        for db_instance_id in db_instance_ids:
            request = rds_20140815_models.DescribeDBInstanceIPArrayListRequest(
                dbinstance_id=db_instance_id
            )
            response = await client.describe_dbinstance_iparray_list_async(request)
            db_instance_ip_allowlist.append(response.body.to_map())
        return db_instance_ip_allowlist
    except Exception as e:
        raise e


@mcp.tool()
async def describe_db_instance_databases(
        region_id: str,
        db_instance_ids: list[str]
) -> list[dict]:
    """
    Batch retrieves database information for multiple RDS instances.
    Args:
        region_id: The region ID of the RDS instance.
        db_instance_ids: List of DB instance identifiers (e.g., ["rm-uf6wjk5****", "db-instance-01"])
    Returns:
        list[dict]: A list of dictionaries containing database information for each instance.
    """
    try:
        client = get_rds_client(region_id)
        db_instance_databases = []
        for db_instance_id in db_instance_ids:
            request = rds_20140815_models.DescribeDatabasesRequest(
                dbinstance_id=db_instance_id
            )
            response = await client.describe_databases_async(request)
            db_instance_databases.append(response.body.to_map())
        return db_instance_databases
    except Exception as e:
        raise e


@mcp.tool()
async def describe_db_instance_accounts(
        region_id: str,
        db_instance_ids: list[str]
) -> list[dict]:
    """
    Batch retrieves account information for multiple RDS instances.
    Args:
        region_id: The region ID of the RDS instance.
        db_instance_ids: List of DB instance identifiers (e.g., ["rm-uf6wjk5****", "db-instance-01"])
    Returns:
        list[dict]: A list of dictionaries containing account information for each instance.
    """
    try:
        client = get_rds_client(region_id)
        db_instance_accounts = []
        for db_instance_id in db_instance_ids:
            request = rds_20140815_models.DescribeAccountsRequest(
                dbinstance_id=db_instance_id
            )
            response = await client.describe_accounts_async(request)
            db_instance_accounts.append(response.body.to_map())
        return db_instance_accounts
    except Exception as e:
        raise e


@mcp.tool()
async def create_db_instance_account(
        region_id: str,
        db_instance_id: str,
        account_name: str,
        account_password: str,
        account_description: str = None,
        account_type: str = "Normal"
) -> dict:
    """
    Create a new account for an RDS instance.
    Args:
        region_id: The region ID of the RDS instance.
        db_instance_id: The ID of the RDS instance.
        account_name: The name of the new account.
        account_password: The password for the new account.
        account_description: The description for the new account.
        account_type: The type of the new account. (e.g. Normal,Super)
    Returns:
         dict[str, Any]: The response.
    """
    try:
        client = get_rds_client(region_id)
        request = rds_20140815_models.CreateAccountRequest(
            dbinstance_id=db_instance_id,
            account_name=account_name,
            account_password=account_password,
            account_description=account_description,
            account_type=account_type
        )
        response = await client.create_account_async(request)
        return response.body.to_map()
    except Exception as e:
        raise e


@mcp.tool()
async def describe_db_instance_parameters(
        region_id: str,
        db_instance_ids: list[str],
        paramters: list[str] = None
) -> dict[str, dict[str, Any]]:
    """
    Batch retrieves parameter information for multiple RDS instances.
    Args:
        region_id: The region ID of the RDS instance.
        db_instance_ids: List of DB instance identifiers (e.g., ["rm-uf6wjk5****", "db-instance-01"])
        paramters: List of parameter names (e.g., ["max_connections", "innodb_buffer_pool_size"])
    Returns:
        list[dict]: A list of dictionaries containing parameter information(ParamGroupInfo,ConfigParameters,RunningParameters) foreach instance.
    """
    try:
        client = get_rds_client(region_id)
        db_instance_parameters = {}
        for db_instance_id in db_instance_ids:
            request = rds_20140815_models.DescribeParametersRequest(
                dbinstance_id=db_instance_id
            )
            response = await client.describe_parameters_async(request)
            if paramters:
                response.body.config_parameters.dbinstance_parameter = [
                    config_parameter for config_parameter in response.body.config_parameters.dbinstance_parameter
                    if config_parameter.parameter_name in paramters
                ]
                response.body.running_parameters.dbinstance_parameter = [
                    running_parameter for running_parameter in response.body.running_parameters.dbinstance_parameter
                    if running_parameter.parameter_name in paramters
                ]

                db_instance_parameters[db_instance_id] = {
                    "ParamGroupInfo": response.body.param_group_info.to_map(),
                    "ConfigParameters": json_array_to_csv(response.body.config_parameters.dbinstance_parameter),
                    "RunningParameters": json_array_to_csv(response.body.running_parameters.dbinstance_parameter)
                }
        return db_instance_parameters
    except Exception as e:
        raise e


@mcp.tool()
async def describe_bills(
        billing_cycles: list[str],
        db_instance_id: str = None,
        is_billing_item: bool = False
) -> dict[str, Any]:
    """
    Query the consumption summary of all product instances or billing items for a user within a specific billing period.
    Args:
        billing_cycles: bill cycle YYYY－MM, e.g. 2020-03
        db_instance_id: DB instance id (e.g., "rm-xxx")
        is_billing_item: Whether to pull data according to the billing item dimension.
    Returns:
        str: billing information.
    """
    try:
        client = get_bill_client("cn-hangzhou")
        res = {}
        for billing_cycle in billing_cycles:
            has_next_token = True
            next_token = None
            items = []

            while has_next_token:
                describe_instance_bill_request = bss_open_api_20171214_models.DescribeInstanceBillRequest(
                    billing_cycle=billing_cycle,
                    product_code='rds',
                    is_billing_item=is_billing_item,
                    next_token=next_token
                )
                if db_instance_id:
                    describe_instance_bill_request.db_instance_id = db_instance_id

                response = client.describe_instance_bill(describe_instance_bill_request)
                if not response.body.data:
                    break
                next_token = response.body.data.next_token
                has_next_token = next_token is not None and next_token.strip() != ""
                items.extend(response.body.data.items)
            item_filters = []
            for item in items:
                if db_instance_id is None or db_instance_id in item.instance_id.split(";"):
                    item_filters.append(
                        {
                            "Item": item.item,
                            "AfterDiscountAmount": item.after_discount_amount,
                            "InstanceID": item.instance_id,
                            "BillingDate": item.billing_date,
                            "InvoiceDiscount": item.invoice_discount,
                            "SubscriptionType": item.subscription_type,
                            "PretaxGrossAmount": item.pretax_gross_amount,
                            "Currency": item.currency,
                            "CommodityCode": item.commodity_code,
                            "CostUnit": item.cost_unit,
                            "NickName": item.nick_name,
                            "PretaxAmount": item.pretax_amount,
                            "BillingItem": item.billing_item,
                            "BillingItemPriceUnit": item.list_price_unit,
                            "BillingItemUsage": item.usage,
                        }
                    )
            res[billing_cycle] = json_array_to_csv(item_filters)
        return res
    except Exception as e:
        raise e


@mcp.tool()
async def modify_db_instance_description(
        region_id: str,
        db_instance_id: str,
        description: str
):
    """
    modify db instance description.
    Args:
        region_id: The region ID of the RDS instance.
        db_instance_id: The ID of the RDS instance.
        description: The RDS instance description.
    Returns:
        dict[str, Any]: The response.
    """
    try:
        client = get_rds_client(region_id)
        request = rds_20140815_models.ModifyDBInstanceDescriptionRequest(
            dbinstance_id=db_instance_id,
            dbinstance_description=description
        )
        response = await client.modify_dbinstance_description_async(request)
        return response.body.to_map()
    except Exception as e:
        raise e


@mcp.tool()
async def allocate_instance_public_connection(
        region_id: str,
        db_instance_id: str,
        connection_string_prefix: str = None,
        port: str = '3306'
):
    """
    allocate db instance public connection.
    Args:
        region_id: The region ID of the RDS instance.
        db_instance_id: The ID of the RDS instance.
        connection_string_prefix: The prefix of connection string.
    Returns:
        dict[str, Any]: The response.
    """
    try:
        if connection_string_prefix is None:
            connection_string_prefix = db_instance_id + "-public"
        client = get_rds_client(region_id)
        request = rds_20140815_models.AllocateInstancePublicConnectionRequest(
            dbinstance_id=db_instance_id,
            connection_string_prefix=connection_string_prefix,
            port=port
        )
        response = await client.allocate_instance_public_connection_async(request)
        return response.body.to_map()
    except Exception as e:
        raise e


@mcp.tool()
async def describe_all_whitelist_template(
        region_id: str,
        template_name: str = None
) -> List[Dict[str, Any]]:
    """
    describe all whitelist template.
    Args:
        region_id: The region ID of the RDS instance.
        template_name: The ID of the RDS instance.
    Returns:
        List[Dict[str, Any]]: The response contains all whitelist template information.
    """
    try:
        client = get_rds_client(region_id)
        next_pages = True
        all_whitelists = []
        page_num = 1
        while next_pages:
            request = rds_20140815_models.DescribeAllWhitelistTemplateRequest(
                template_name=template_name,
                fuzzy_search=False if template_name is None else True,
                max_records_per_page=100,
                page_numbers=page_num
            )
            response = await client.describe_all_whitelist_template_async(request)
            next_pages = response.body.data.has_next
            page_num += 1
            all_whitelists.extend(response.body.data.templates)
        return [item.to_map() for item in all_whitelists]
    except Exception as e:
        raise e


@mcp.tool()
async def describe_instance_linked_whitelist_template(
        region_id: str,
        db_instance_id: str
):
    """
    describe instance linked whitelist template.
    Args:
        region_id: The region ID of the RDS instance.
        db_instance_id: The ID of the RDS instance.
    Returns:
        dict[str, Any]: The response.
    """
    try:
        client = get_rds_client(region_id)
        request = rds_20140815_models.DescribeInstanceLinkedWhitelistTemplateRequest(
            region_id=region_id,
            ins_name=db_instance_id
        )
        response = await client.describe_instance_linked_whitelist_template_async(request)
        return response.body.to_map()
    except Exception as e:
        raise e


@mcp.tool()
async def attach_whitelist_template_to_instance(
        region_id: str,
        db_instance_id: str,
        template_id: int
):
    """
    allocate db instance public connection.
    Args:
        region_id: The region ID of the RDS instance.
        db_instance_id: The ID of the RDS instance.
        template_id: Whitelist Template ID. Can be obtained via DescribeAllWhitelistTemplate.
    Returns:
        dict[str, Any]: The response.
    """
    try:
        client = get_rds_client(region_id)
        request = rds_20140815_models.AttachWhitelistTemplateToInstanceRequest(
            region_id=region_id,
            ins_name=db_instance_id,
            template_id=template_id
        )
        response = await client.attach_whitelist_template_to_instance_async(request)
        return response.body.to_map()
    except Exception as e:
        raise e


@mcp.tool()
async def add_tags_to_db_instance(
        region_id: str,
        db_instance_id: str,
        tags: Dict[str, str]
):
    """
    add tags to db instance.
    Args:
        region_id: The region ID of the RDS instance.
        db_instance_id: The ID of the RDS instance.
        tags: The tags to be added to the RDS instance.
    Returns:
        dict[str, Any]: The response.
    """
    try:
        client = get_rds_client(region_id)
        request = rds_20140815_models.AddTagsToResourceRequest(
            region_id=region_id,
            dbinstance_id=db_instance_id,
            tags=json.dumps(tags)
        )
        response = await client.add_tags_to_resource_async(request)
        return response.body.to_map()
    except Exception as e:
        raise e


@mcp.tool()
async def get_current_time() -> Dict[str, Any]:
    """Get the current time.

    Returns:
        Dict[str, Any]: The response containing the current time.
    """
    try:
        # Get the current time
        current_time = datetime.now()

        # Format the current time as a string
        formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S")

        # Return the response
        return {
            "current_time": formatted_time
        }
    except Exception as e:
        logger.error(f"Error occurred while getting the current time: {str(e)}")
        raise Exception(f"Failed to get the current time: {str(e)}")


@mcp.tool()
async def modify_security_ips(
        region_id: str,
        dbinstance_id: str,
        security_ips: str,
        whitelist_network_type: str = "MIX",
        security_ip_type: str = None,
        dbinstance_ip_array_name: str = None,
        dbinstance_ip_array_attribute: str = None,
        client_token: str = None
) -> Dict[str, Any]:
    """modify security ips。

    Args:
        region_id (str): RDS instance region id.
        dbinstance_id (str): RDS instance id.
        security_ips (str): security ips list, separated by commas.
        whitelist_network_type (str, optional): whitelist network type.
            - MIX: mixed network type
            - Classic: classic network
            - VPC: vpc
            default value: MIX
        security_ip_type (str, optional): security ip type.
            - normal: normal security ip
            - hidden: hidden security ip
        dbinstance_ip_array_name (str, optional): security ip array name.
        dbinstance_ip_array_attribute (str, optional): security ip array attribute.
            - hidden: hidden security ip
            - normal: normal security ip
        client_token (str, optional): idempotency token, max 64 ascii characters.

    Returns:
        Dict[str, Any]: response contains request id.
    """
    try:
        # initialize client
        client = get_rds_client(region_id)

        # create request
        request = rds_20140815_models.ModifySecurityIpsRequest(
            dbinstance_id=dbinstance_id,
            security_ips=security_ips,
            whitelist_network_type=whitelist_network_type
        )

        # add optional parameters
        if security_ip_type:
            request.security_ip_type = security_ip_type
        if dbinstance_ip_array_name:
            request.dbinstance_ip_array_name = dbinstance_ip_array_name
        if dbinstance_ip_array_attribute:
            request.dbinstance_ip_array_attribute = dbinstance_ip_array_attribute
        if client_token:
            request.client_token = client_token

        # send api request
        response = client.modify_security_ips(request)
        return response.body.to_map()

    except Exception as e:
        logger.error(f"modify security ips error: {str(e)}")
        raise OpenAPIError(f"modify rds instance security ips failed: {str(e)}")


@mcp.tool()
async def restart_db_instance(
        region_id: str,
        dbinstance_id: str,
        effective_time: str = "Immediate",
        switch_time: str = None,
        client_token: str = None
) -> Dict[str, Any]:
    """Restart an RDS instance.

    Args:
        region_id (str): The region ID of the RDS instance.
        dbinstance_id (str): The ID of the RDS instance.
        effective_time (str, optional): When to restart the instance. Options:
            - Immediate: Restart immediately
            - MaintainTime: Restart during maintenance window
            - ScheduleTime: Restart at specified time
            Default: Immediate
        switch_time (str, optional): The scheduled restart time in format: yyyy-MM-ddTHH:mm:ssZ (UTC time).
            Required when effective_time is ScheduleTime.
        client_token (str, optional): Idempotency token, max 64 ASCII characters.

    Returns:
        Dict[str, Any]: Response containing the request ID.
    """
    try:
        # Initialize client
        client = get_rds_client(region_id)

        # Create request
        request = rds_20140815_models.RestartDBInstanceRequest(
            dbinstance_id=dbinstance_id
        )

        # Add optional parameters
        if effective_time:
            request.effective_time = effective_time
        if switch_time:
            request.switch_time = switch_time
        if client_token:
            request.client_token = client_token

        # Make the API request
        response = client.restart_dbinstance(request)
        return response.body.to_map()

    except Exception as e:
        logger.error(f"Error occurred while restarting instance: {str(e)}")
        raise OpenAPIError(f"Failed to restart RDS instance: {str(e)}")


@mcp.tool()
async def describe_sql_insight_statistic(
        dbinstance_id: str,
        start_time: str,
        end_time: str,
) -> Dict[str, Any]:
    """
    Query SQL Log statistics, including SQL cost time, execution times, and account.
    Args:
        dbinstance_id (str): The ID of the RDS instance.
        start_time(str): the start time of sql insight statistic. e.g. 2025-06-06 20:00:00
        end_time(str): the end time of sql insight statistic. e.g. 2025-06-06 20:10:00
    Returns:
        the sql insight statistic information in csv format.
    """
    def _descirbe(order_by: str):
        try:
        # Initialize client
            client = get_das_client()
            page_no = 1
            page_size = 50
            total = page_no * page_size + 1
            result = []
            while total > page_no * page_size:
                state = "RUNNING"
                job_id = ""
                while state == "RUNNING":
                    body = {
                        "InstanceId": dbinstance_id,
                        "OrderBy": order_by,
                        "Asc": False,
                        "PageNo": 1,
                        "PageSize": 10,
                        "TemplateId": "",
                        "DbName": "",
                        "StartTime": convert_datetime_to_timestamp(start_time),
                        "EndTime": convert_datetime_to_timestamp(end_time),
                        "JobId": job_id
                    }
                    req = open_api_models.OpenApiRequest(
                        query=OpenApiUtilClient.query({}),
                        body=OpenApiUtilClient.parse_to_map(body)
                    )
                    params = open_api_models.Params(
                        action='DescribeSqlInsightStatistic',
                        version='2020-01-16',
                        protocol='HTTPS',
                        pathname='/',
                        method='POST',
                        auth_type='AK',
                        style='RPC',
                        req_body_type='formData',
                        body_type='json'
                    )
                    response = client.call_api(params, req, util_models.RuntimeOptions())
                    response_data = response['body']['Data']
                    state = response_data['State']
                    if state == "RUNNING":
                        job_id = response_data['ResultId']
                        time.sleep(1)
                        continue
                    if state == "SUCCESS":
                        result.extend(response_data['Data']['List'])
                        total = response_data['Data']['Total']
                        page_no = page_no + 1

            return json_array_to_csv(result)
        except Exception as e:
            logger.error(f"Error occurred: {str(e)}")
            raise e
    rt_rate = _descirbe("rtRate")
    count_rate = _descirbe("countRate")
    return {
        "sql_log_order_by_rt_rate": rt_rate,
        "sql_log_order_by_count_rate": count_rate
    }


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

