"""PostgreSQL 部署配置与兼容迁移回归测试。"""
from unittest.mock import Mock, patch

import pytest

import database


class FakeInspector:
    def __init__(self, columns_by_table: dict[str, set[str]]):
        self.columns_by_table = columns_by_table

    def get_table_names(self) -> list[str]:
        return list(self.columns_by_table)

    def get_columns(self, table_name: str) -> list[dict[str, str]]:
        return [{"name": name} for name in self.columns_by_table[table_name]]


def executed_sql(connection: Mock) -> list[str]:
    return [str(call.args[0]) for call in connection.execute.call_args_list]


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PLATFORM_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="必须设置 PLATFORM_DATABASE_URL"):
        database._get_async_database_url()


def test_database_url_normalizes_postgresql_driver(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PLATFORM_DATABASE_URL", "postgresql://user:pass@db/platform")
    assert database._get_async_database_url() == "postgresql+asyncpg://user:pass@db/platform"


def test_postgresql_migration_uses_timestamp_type_for_missing_column():
    connection = Mock()
    inspector = FakeInspector({"scripts": set()})

    with patch.object(database, "inspect", return_value=inspector):
        database._sync_migrate(connection)

    sql = executed_sql(connection)
    assert "ALTER TABLE scripts ADD COLUMN pinned_at TIMESTAMP WITHOUT TIME ZONE" in sql
    assert all("DATETIME" not in statement for statement in sql)


def test_postgresql_migration_is_noop_when_no_tables_exist():
    connection = Mock()
    inspector = FakeInspector({})

    with patch.object(database, "inspect", return_value=inspector):
        database._sync_migrate(connection)

    connection.execute.assert_not_called()
