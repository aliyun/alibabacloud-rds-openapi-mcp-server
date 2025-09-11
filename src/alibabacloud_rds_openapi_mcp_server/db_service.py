import asyncio
import json
import random
import socket
import string

import pymysql
from alibabacloud_rds20140815 import models as rds_20140815_models

from utils import get_rds_client, get_rds_account


def random_str(length=8):
    chars = string.ascii_lowercase + string.digits
    return ''.join(random.choice(chars) for _ in range(length))


def random_password(length=32):
    U = string.ascii_uppercase
    L = string.ascii_lowercase
    D = string.digits
    S = '_!@#$%^&*()-+='
    pool = U + L + D + S
    for _ in range(1000):
        # 确保至少三类
        chosen = [
            random.choice(U),
            random.choice(L),
            random.choice(D),
            random.choice(S)
        ]
        rest = [random.choice(pool) for _ in range(length - len(chosen))]
        pw = ''.join(random.sample(chosen + rest, length))
    return pw


def test_connect(host, port, timeout=1):
    try:
        with socket.create_connection((host, int(port)), timeout):
            return True
    except Exception:
        return False


class DBService:
    """
    Create a read-only account, execute the SQL statements, and automatically delete the account afterward.
    """
    def __init__(self,
                 region_id,
                 instance_id,
                 database=None,
                 privilege_databases=None):
        self.instance_id = instance_id
        self.database = database
        self.region_id = region_id
        self.privilege_databases = privilege_databases

        self.__db_type = None
        self.__account_name, self.__account_password = get_rds_account()
        self.__host = None
        self.__port = None
        self.__client = get_rds_client(region_id)
        self.__db_conn = None

    async def __aenter__(self):
        await asyncio.to_thread(self._get_db_instance_info)
        if not self.__account_name or not self.__account_password:
            await asyncio.to_thread(self._create_temp_account)
            if self.database:
                await asyncio.to_thread(self._grant_privilege)
        else:
            self.account_name = self.__account_name
            self.account_password = self.__account_password
        self.__db_conn = DBConn(self)
        await asyncio.to_thread(self.__db_conn.connect)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.__db_conn is not None:
            await asyncio.to_thread(self.__db_conn.close)
        if not self.__account_name or not self.__account_password:
            await asyncio.to_thread(self._delete_account)
        self.__client = None

    def _get_db_instance_info(self):
        req = rds_20140815_models.DescribeDBInstanceAttributeRequest(
            dbinstance_id=self.instance_id,
        )
        self.__client.describe_dbinstance_attribute(req)
        resp = self.__client.describe_dbinstance_attribute(req)
        self.db_type = resp.body.items.dbinstance_attribute[0].engine.lower()

        req = rds_20140815_models.DescribeDBInstanceNetInfoRequest(
            dbinstance_id=self.instance_id,
        )
        resp = self.__client.describe_dbinstance_net_info(req)

        # 取支持的地址:
        vpc_host, vpc_port, public_host, public_port, dbtype = None, None, None, None, None
        net_infos = resp.body.dbinstance_net_infos.dbinstance_net_info
        for item in net_infos:
            if 'Private' == item.iptype:
                vpc_host = item.connection_string
                vpc_port = int(item.port)
            elif 'Public' in item.iptype:
                public_host = item.connection_string
                public_port = int(item.port)

        if vpc_host and test_connect(vpc_host, vpc_port):
            self.host = vpc_host
            self.port = vpc_port
        elif public_host and test_connect(public_host, public_port):
            self.host = public_host
            self.port = public_port
        else:
            raise Exception('connection db failed.')

    def _create_temp_account(self):
        self.account_name = 'mcp_' + random_str(10)
        self.account_password = random_password(32)
        request = rds_20140815_models.CreateAccountRequest(
            dbinstance_id=self.instance_id,
            account_name=self.account_name,
            account_password=self.account_password,
            account_description="Created by mcp for execute sql."
        )
        self.__client.create_account(request)

    def _grant_privilege(self):
        if self.privilege_databases:
            dbname = self.privilege_databases
            cnt = len(self.privilege_databases.split(','))
            if self.db_type.lower() in ('mysql', 'postgresql'):
                privilege = ",".join(['ReadOnly' for i in range(cnt)])
            else:
                privilege = ",".join(['DBOwner' for i in range(cnt)])
        else:
            dbname = self.database
            privilege = "ReadOnly" if self.db_type.lower() in ('mysql', 'postgresql') else "DBOwner"

        req = rds_20140815_models.GrantAccountPrivilegeRequest(
            dbinstance_id=self.instance_id,
            account_name=self.account_name,
            dbname=dbname,
            account_privilege=privilege
        )
        self.__client.grant_account_privilege(req)

    def _delete_account(self):
        if not self.account_name:
            return
        req = rds_20140815_models.DeleteAccountRequest(
            dbinstance_id=self.instance_id,
            account_name=self.account_name
        )
        self.__client.delete_account(req)

    async def execute_sql(self, sql):
        return await asyncio.to_thread(self.__db_conn.execute_sql, sql)

    @property
    def user(self):
        return self.account_name

    @property
    def password(self):
        return self.account_password


class DBConn:
    def __init__(self, db_service: DBService):
        self.dbtype = db_service.db_type
        self.host = db_service.host
        self.port = db_service.port
        self.user = db_service.user
        self.password = db_service.password
        self.database = db_service.database
        self.conn = None

    def connect(self):
        if self.conn is not None:
            return

        if self.dbtype == 'mysql':
            self.conn = pymysql.connect(
                host=self.host, port=self.port,
                user=self.user, password=self.password,
                db=self.database, charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor
            )
        elif self.dbtype == 'postgresql' or self.dbtype == 'pg':
            import psycopg2
            self.conn = psycopg2.connect(
                host=self.host, port=self.port,
                user=self.user, password=self.password,
                dbname=self.database
            )
        elif self.dbtype == 'sqlserver':
            import pyodbc
            driver = 'ODBC Driver 17 for SQL Server'
            conn_str = (
                f'DRIVER={{{driver}}};SERVER={self.host},{self.port};'
                f'UID={self.user};PWD={self.password};DATABASE={self.database}'
            )
            self.conn = pyodbc.connect(conn_str)
        else:
            raise ValueError('Unsupported dbtype')

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception as e:
                print(e)
            self.conn = None

    def execute_sql(self, sql):
        cursor = self.conn.cursor()
        cursor.execute(sql)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        if self.dbtype == 'mysql':
            result = [dict(row) for row in rows]
        elif self.dbtype == 'postgresql' or self.dbtype == 'pg':
            result = [dict(zip(columns, row)) for row in rows]
        elif self.dbtype == 'sqlserver':
            result = [dict(zip(columns, row)) for row in rows]
        else:
            result = []
        cursor.close()
        try:
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return str(result)
