import sys
import random
from pathlib import Path

import pytest

package_dir = Path(__file__).resolve().parents[1] / "src" / "alibabacloud_rds_openapi_mcp_server"
sys.path.insert(0, str(package_dir))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alibabacloud_rds_openapi_mcp_server import db_service


class FakeRdsClient:
    def __init__(self):
        self.grant_requests = []

    def grant_account_privilege(self, request):
        self.grant_requests.append(request)


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeRdsClient()
    monkeypatch.setattr(db_service, "get_rds_client", lambda _region_id: client)
    monkeypatch.setattr(db_service, "get_rds_account", lambda: (None, None))
    return client


def _make_service(fake_client, database="app_db", privilege_databases=None):
    service = db_service.DBService("cn-hangzhou", "rm-test", database, privilege_databases)
    service.db_type = "sqlserver"
    service.account_name = "mcp_readonly"
    service._DBService__client = fake_client
    return service


def test_sqlserver_temp_account_gets_readonly_privilege(fake_client):
    service = _make_service(fake_client)

    service._grant_privilege()

    request = fake_client.grant_requests[0]
    assert request.dbname == "app_db"
    assert request.account_privilege == "ReadOnly"


def test_sqlserver_multi_database_temp_account_gets_readonly_privileges(fake_client):
    service = _make_service(fake_client, database="information_schema", privilege_databases="db1,db2")

    service._grant_privilege()

    request = fake_client.grant_requests[0]
    assert request.dbname == "db1,db2"
    assert request.account_privilege == "ReadOnly,ReadOnly"


def test_random_str_does_not_use_pseudo_random_module(monkeypatch):
    def insecure_choice(*_args, **_kwargs):
        raise AssertionError("random.choice must not be used for generated account names")

    monkeypatch.setattr(random, "choice", insecure_choice)

    generated = db_service.random_str(12)

    assert len(generated) == 12
    assert generated.isalnum()


def test_random_password_does_not_use_pseudo_random_module(monkeypatch):
    def insecure_choice(*_args, **_kwargs):
        raise AssertionError("random.choice must not be used for generated passwords")

    def insecure_sample(*_args, **_kwargs):
        raise AssertionError("random.sample must not be used for generated passwords")

    monkeypatch.setattr(random, "choice", insecure_choice)
    monkeypatch.setattr(random, "sample", insecure_sample)

    generated = db_service.random_password(32)

    assert len(generated) == 32
    assert any(ch.isupper() for ch in generated)
    assert any(ch.islower() for ch in generated)
    assert any(ch.isdigit() for ch in generated)
    assert any(ch in "_!@#$%^&*()-+=" for ch in generated)
