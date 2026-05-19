import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alibabacloud_rds_openapi_mcp_server import server
from alibabacloud_rds_openapi_mcp_server.sql_safety import SqlSafetyValidator


class FakeDBService:
    calls = []

    def __init__(self, *args):
        self.args = args

    async def __aenter__(self):
        FakeDBService.calls.append(("enter", self.args))
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def execute_sql(self, sql):
        FakeDBService.calls.append(("execute", sql))
        return sql


@pytest.fixture(autouse=True)
def fake_db_service(monkeypatch):
    FakeDBService.calls = []
    monkeypatch.setattr(server, "DBService", FakeDBService)


def test_show_create_table_quotes_valid_identifiers():
    result = asyncio.run(
        server.show_create_table("cn-hangzhou", "rm-test", "app_db", "orders")
    )

    assert result == "show create table `app_db`.`orders`"


def test_show_create_table_rejects_identifier_injection():
    with pytest.raises(ValueError, match="Invalid table_name"):
        asyncio.run(
            server.show_create_table(
                "cn-hangzhou",
                "rm-test",
                "app_db",
                "orders; drop table users",
            )
        )

    assert FakeDBService.calls == []


def test_query_sql_rejects_multi_statement_sql():
    with pytest.raises(ValueError, match="single read-only statement"):
        asyncio.run(
            server.query_sql(
                "cn-hangzhou",
                "rm-test",
                "app_db",
                "select * from users; drop table users",
            )
        )

    assert FakeDBService.calls == []


def test_query_sql_rejects_write_sql():
    with pytest.raises(ValueError, match="Only read-only SQL"):
        asyncio.run(
            server.query_sql(
                "cn-hangzhou",
                "rm-test",
                "app_db",
                "delete from users",
            )
        )

    assert FakeDBService.calls == []


def test_query_sql_rejects_too_long_sql():
    with pytest.raises(ValueError, match="SQL is too long"):
        asyncio.run(
            server.query_sql(
                "cn-hangzhou",
                "rm-test",
                "app_db",
                "select " + "a" * (SqlSafetyValidator.MAX_SQL_LENGTH + 1),
            )
        )

    assert FakeDBService.calls == []


def test_query_sql_allows_select_sql():
    result = asyncio.run(
        server.query_sql("cn-hangzhou", "rm-test", "app_db", "select * from users")
    )

    assert result == "select * from users"


def test_explain_sql_rejects_non_select_sql():
    with pytest.raises(ValueError, match="EXPLAIN only supports SELECT"):
        asyncio.run(
            server.explain_sql(
                "cn-hangzhou",
                "rm-test",
                "app_db",
                "update users set role = 'admin'",
            )
        )

    assert FakeDBService.calls == []


def test_explain_sql_allows_select_sql():
    result = asyncio.run(
        server.explain_sql("cn-hangzhou", "rm-test", "app_db", "select * from users")
    )

    assert result == "explain select * from users"
