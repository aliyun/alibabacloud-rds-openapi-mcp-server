import csv
import os
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
from io import StringIO
import tzlocal
import time
import json

from alibabacloud_bssopenapi20171214.client import Client as BssOpenApi20171214Client
from alibabacloud_rds20140815.client import Client as RdsClient
from alibabacloud_tea_openapi.models import Config
from alibabacloud_vpc20160428.client import Client as VpcClient
from alibabacloud_das20200116.client import Client as DAS20200116Client

current_request_headers: ContextVar[dict] = ContextVar("current_request_headers", default={})

PERF_KEYS = {
    "mysql": {
        "MemCpuUsage": ["MySQL_MemCpuUsage"],
        "QPSTPS": ["MySQL_QPSTPS"],
        "Sessions": ["MySQL_Sessions"],
        "COMDML": ["MySQL_COMDML"],
        "RowDML": ["MySQL_RowDML"],
        "SpaceUsage": ["MySQL_DetailedSpaceUsage"],
        "ThreadStatus": ["MySQL_ThreadStatus"],
        "MBPS": ["MySQL_MBPS"],
        "DetailedSpaceUsage": ["MySQL_DetailedSpaceUsage"]
    },
    "pgsql": {
        "MemCpuUsage": ["MemoryUsage", "CpuUsage"],
        "QPSTPS": ["PolarDBQPSTPS"],
        "Sessions": ["PgSQL_Session"],
        "COMDML": ["PgSQL_COMDML"],
        "RowDML": ["PolarDBRowDML"],
        "SpaceUsage": ["PgSQL_SpaceUsage"],
        "ThreadStatus": [],
        "MBPS": [],
        "DetailedSpaceUsage": ["SQLServer_DetailedSpaceUsage"]
    },
    "sqlserver": {
        "MemCpuUsage": ["SQLServer_CPUUsage"],
        "QPSTPS": ["SQLServer_QPS", "SQLServer_IOPS"],
        "Sessions": ["SQLServer_Sessions"],
        "COMDML": [],
        "RowDML": [],
        "SpaceUsage": ["SQLServer_DetailedSpaceUsage"],
        "ThreadStatus": [],
        "MBPS": [],
        "DetailedSpaceUsage": ["PgSQL_SpaceUsage"]
    }

}

DAS_KEYS = {
    "mysql": {
        "DiskUsage": ["disk_usage"],
        "IOPSUsage": ["data_iops_usage"],
        "IOBytesPS": ["data_io_bytes_ps"],
        "MdlLockSession": ["mdl_lock_session"],
        "RelayLogSize": ["relay_log_size"],
        "UndoLogSize": ["undolog_size"],
        "RedoLog_Size": ["redolog_size"],
        "TempFileSize": ["temp_file_size"],
        "InsSize": ["ins_size"],
        "SysDataSize": ["sys_data_size"],
        "GeneralLogSize": ["general_log_size"],
        "SlowLogSize": ["slow_log_size"],
        "BinlogSize": ["binlog_size"],
        "UserDataSize": ["user_data_size"],
        "InnodbRowsInsert":["Innodb_rows_inserted_ps"],
        "MemCpuUsage": ["cpu_usage"],
        "QPS": ["qps"],
        "SLowSQL": ["slow_sql"]
    }
}


def parse_args(argv):
    args = {}
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg.startswith('--'):
            key = arg[2:]
            if i + 1 < len(argv) and not argv[i + 1].startswith('--'):
                args[key] = argv[i+1]
                i += 2
            else:
                args[key] = True
                i += 1
    return args


def transform_to_iso_8601(dt: datetime, timespec: str):
    return dt.astimezone(timezone.utc).isoformat(timespec=timespec).replace("+00:00", "Z")

def parse_iso_8601(s: str) -> datetime:
    """
    将 ISO 8601 格式字符串（支持 Z 时区标记）转换为 datetime 对象。
    """
    # 替换 'Z' 为 '+00:00'，以便正确解析为 UTC 时间
    s = s.replace("Z", "+00:00")
    # 解析字符串为 UTC 时间的 datetime 对象
    dt_utc = datetime.fromisoformat(s)
    # 获取本地时区
    local_tz = tzlocal.get_localzone()
    # 转换为本地时区时间
    dt_local = dt_utc.astimezone(local_tz)
    return dt_local.replace(tzinfo=None)

def transform_timestamp_to_datetime(timestamp: int):
    dt = datetime.fromtimestamp(timestamp / 1000)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def transform_to_datetime(s: str):
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
    return dt


def transform_perf_key(db_type: str, perf_keys: list[str]):
    perf_key_after_transform = []
    for key in perf_keys:
        if key in PERF_KEYS[db_type.lower()]:
            perf_key_after_transform.extend(PERF_KEYS[db_type.lower()][key])
        else:
            perf_key_after_transform.append(key)
    return perf_key_after_transform

def transform_das_key(db_type: str, das_keys: list[str]):
    das_key_after_transform = []
    for key in das_keys:
        if key in DAS_KEYS[db_type.lower()]:
            das_key_after_transform.extend(DAS_KEYS[db_type.lower()][key])
        else:
            das_key_after_transform.append(key)
    return das_key_after_transform


def json_array_to_csv(data):
    if not data or not isinstance(data, list):
        return ""

    fieldnames = set()
    for item in data:
        if isinstance(item, dict):
            fieldnames.update(item.keys())
        elif hasattr(item, 'to_map'):
            fieldnames.update(item.to_map().keys())

    if not fieldnames:
        return ""

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=sorted(fieldnames))

    writer.writeheader()
    for item in data:
        if isinstance(item, dict):
            writer.writerow({k: v if v is not None else '' for k, v in item.items()})
        elif hasattr(item, 'to_map'):
            writer.writerow({k: v if v is not None else '' for k, v in item.to_map().items()})

    return output.getvalue()


def json_array_to_markdown(headers, datas):
    if not headers or not isinstance(headers, list):
        return ""
    if not datas or not isinstance(datas, list):
        return ""
    
    markdown_table = "| " + " | ".join(headers) + " |\n"
    markdown_table += "| " + " | ".join(["---"] * len(headers)) + " |\n"
    for row in datas:
        if isinstance(row, dict):
            markdown_table += "| " + " | ".join(str(row.get(header, '-')) for header in headers) + " |\n"
        else:
            markdown_table += "| " + " | ".join(map(str, row)) + " |\n"
    return markdown_table

def json_exec_sql(json_data, sql: str):
    """
    将JSON格式的数据导入SQLite数据库的result表中，然后执行指定的SQL查询并返回结果。
    自动推断数据类型以保留数据格式。
    
    Args:
        json_data (list): JSON数据列表，每个元素是一个字典
        sql (str): 要在导入的数据上执行的SQL查询语句
        
    Returns:
        list: SQL查询结果的列表，每个元素是一个字典，表示一行数据
    """
    import sqlite3
    import tempfile
    import os
    import json
    
    if not json_data or not isinstance(json_data, list):
        return []
    
    # 创建临时SQLite数据库文件
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    
    try:
        # 连接到SQLite数据库
        conn = sqlite3.connect(temp_db.name)
        conn.row_factory = sqlite3.Row  # 使结果可以通过列名访问
        cursor = conn.cursor()
        
        process_data = _process_json_data_for_sql(json_data)
        if not process_data:
            return []

        # 分析数据类型
        column_types = _analyze_json_data_types(process_data)
        
        if not column_types:
            return []
        
        # 创建result表，使用推断的数据类型
        columns_def = ', '.join([f'"{col}" {col_type}' for col, col_type in column_types.items()])
        cursor.execute(f"CREATE TABLE result ({columns_def})")
        
        # 准备插入数据
        columns = list(column_types.keys())
        placeholders = ', '.join(['?' for _ in columns])
        insert_sql = f"INSERT INTO result VALUES ({placeholders})"
        
        # 转换数据并插入
        for item in process_data:
            if isinstance(item, dict):
                row_data = []
                for col in columns:
                    value = item.get(col)
                    # 确保数据类型匹配
                    if isinstance(value, (dict, list)) and column_types[col] == 'JSON':
                        value = json.dumps(value, ensure_ascii=False)
                    elif value is not None and column_types[col] == 'INTEGER':
                        try:
                            value = int(value)
                        except (ValueError, TypeError):
                            value = ''
                    elif value is not None and column_types[col] == 'REAL':
                        try:
                            value = float(value)
                        except (ValueError, TypeError):
                            value = ''
                    elif value is None:
                        value = ''
                    row_data.append(value)
                cursor.execute(insert_sql, row_data)
        
        # 提交事务
        conn.commit()
        
        # 执行查询SQL
        cursor.execute(sql)
        rows = cursor.fetchall()
        
        # 将结果转换为字典列表
        result = [dict(row) for row in rows]
        
        return result
        
    finally:
        # 关闭数据库连接
        if 'conn' in locals():
            conn.close()
        # 删除临时数据库文件
        os.unlink(temp_db.name)

def convert_datetime_to_timestamp(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    timestamp_seconds = time.mktime(dt.timetuple())
    timestamp_milliseconds = int(timestamp_seconds) * 1000
    return timestamp_milliseconds

def utc_to_localtime_format(time_str):
    """
    UTC时间转成+8时区
    """
    utc_time = datetime.fromisoformat(time_str.replace("Z", "+00:00"))

    # 转换为 UTC+8
    utc_plus_8 = timezone(timedelta(hours=8))
    time_utc_8 = utc_time.astimezone(utc_plus_8)

    # 格式化为 "YYYY-MM-DD HH:MM:SS"
    formatted = time_utc_8.strftime("%Y-%m-%d %H:%M:%S")
    return formatted

def get_rds_account():
    header = current_request_headers.get()
    user = header.get("rds_user") if header else None
    passwd = header.get("rds_passwd") if header else None
    if user and passwd:
        return user, passwd
    return None, None


def get_aksk():
    ak = os.getenv('ALIBABA_CLOUD_ACCESS_KEY_ID')
    sk = os.getenv('ALIBABA_CLOUD_ACCESS_KEY_SECRET')
    sts = os.getenv('ALIBABA_CLOUD_SECURITY_TOKEN')
    header = current_request_headers.get()
    if header and (header.get("ak") or header.get("sk") or header.get("sts")):
        ak, sk, sts = header.get("ak"), header.get("sk"), header.get("sts")
    return ak, sk, sts


def get_rds_client(region_id: str):
    ak, sk, sts = get_aksk()
    config = Config(
        access_key_id=ak,
        access_key_secret=sk,
        security_token=sts,
        region_id=region_id,
        protocol="https",
        connect_timeout=10 * 1000,
        read_timeout=300 * 1000
    )
    client = RdsClient(config)
    return client


def get_vpc_client(region_id: str) -> VpcClient:
    """Get VPC client instance.

    Args:
        region_id: The region ID for the VPC client.

    Returns:
        VpcClient: The VPC client instance for the specified region.
    """
    ak, sk, sts = get_aksk()
    config = Config(
        access_key_id=ak,
        access_key_secret=sk,
        security_token=sts,
        region_id=region_id,
        protocol="https",
        connect_timeout=10 * 1000,
        read_timeout=300 * 1000
    )
    return VpcClient(config)


def get_bill_client(region_id: str):
    ak, sk, sts = get_aksk()
    config = Config(
        access_key_id=ak,
        access_key_secret=sk,
        security_token=sts,
        region_id=region_id,
        protocol="https",
        connect_timeout=10 * 1000,
        read_timeout=300 * 1000
    )
    client = BssOpenApi20171214Client(config)
    return client


def get_das_client():
    ak, sk, sts = get_aksk()
    config = Config(
        access_key_id=ak,
        access_key_secret=sk,
        security_token=sts,
        region_id='cn-shanghai',
        protocol="https",
        connect_timeout=10 * 1000,
        read_timeout=300 * 1000
    )
    client = DAS20200116Client(config)
    return client

def _infer_sqlite_type(value):
    """
    根据Python值推断SQLite数据类型
    
    Args:
        value: Python值
        
    Returns:
        str: SQLite数据类型
    """
    if value is None:
        return 'TEXT'
    elif isinstance(value, bool):
        return 'BOOLEAN'  # SQLite用INTEGER存储布尔值
    elif isinstance(value, int):
        return 'INTEGER'
    elif isinstance(value, float):
        return 'REAL'
    elif isinstance(value, (dict, list)):
        return 'JSON'
    else:
        return 'TEXT'

def _process_json_data_for_sql(json_data):
    """
    预处理JSON数据，将复杂数据结构转换为适合SQLite存储的格式
    仿照json_array_to_csv的处理方式
    
    Args:
        json_data (list): JSON数据列表
        
    Returns:
        list: 处理后的数据列表
    """
    if not json_data or not isinstance(json_data, list):
        return []
    
    processed_data = []
    for item in json_data:
        if isinstance(item, dict):
            processed_item = {}
            for key, value in item.items():
                # 处理复杂数据结构
                if value is None:
                    processed_item[key] = ''
                elif hasattr(value, 'to_map'):
                    # 处理有to_map方法的对象
                    processed_item[key] = json.dumps(value.to_map(), ensure_ascii=False)
                else:
                    processed_item[key] = value
            processed_data.append(processed_item)
        elif hasattr(item, 'to_map'):
            # 处理有to_map方法的对象
            processed_item = {}
            for key, value in item.to_map().items():
                if value is None:
                    processed_item[key] = ''
                elif isinstance(value, (dict, list)):
                    processed_item[key] = json.dumps(value, ensure_ascii=False)
                else:
                    processed_item[key] = value
            processed_data.append(processed_item)
        else:
            # 其他类型直接添加
            processed_data.append(item)
    
    return processed_data

def _analyze_json_data_types(json_data):
    """
    分析JSON数据中每列的数据类型
    
    Args:
        json_data (list): JSON数据列表
        
    Returns:
        dict: 列名到数据类型的映射
    """
    column_types = {}
    
    for item in json_data:
        if isinstance(item, dict):
            for key, value in item.items():
                inferred_type = _infer_sqlite_type(value)
                
                # 如果该列还没有类型，或者当前类型更具体，则更新
                if key not in column_types:
                    column_types[key] = inferred_type
                else:
                    # 类型优先级：INTEGER > REAL > TEXT
                    current_type = column_types[key]
                    if current_type == 'TEXT' and inferred_type in ['INTEGER', 'REAL']:
                        column_types[key] = inferred_type
                    elif current_type == 'REAL' and inferred_type == 'INTEGER':
                        column_types[key] = 'INTEGER'
    
    return column_types