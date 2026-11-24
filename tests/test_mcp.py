"""The MCP server wrappers, exercised with a fake SDK so the test never skips."""

import sys
import types
from datetime import date

import pytest

from finance_tool.store.entities import (
    Account,
    Settings,
    StoreDocument,
    Transaction,
)
from finance_tool.store.store import Store

TODAY = date(2026, 11, 5)


class FakeMCPServer:
    def __init__(self, name):
        self.name = name
        self.tools = {}
        self.resources = {}

    def tool(self):
        def decorate(function):
            self.tools[function.__name__] = function
            return function
        return decorate

    def resource(self, uri):
        def decorate(function):
            self.resources[uri] = function
            return function
        return decorate

    def run(self):
        pass


def _install_fake(monkeypatch):
    package = types.ModuleType("mcp")
    server = types.ModuleType("mcp.server")
    mcpserver = types.ModuleType("mcp.server.mcpserver")
    mcpserver.MCPServer = FakeMCPServer
    server.mcpserver = mcpserver
    package.server = server
    monkeypatch.setitem(sys.modules, "mcp", package)
    monkeypatch.setitem(sys.modules, "mcp.server", server)
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", mcpserver)


def _store(tmp_path, **settings):
    settings.setdefault("ai_writes", True)
    return Store(data_dir=tmp_path, doc=StoreDocument(
        accounts=[Account(id="a_1", name="Everyday", kind="transaction",
                          balance=100.0, currency="AUD")],
        transactions=[Transaction(id="t1", date=date(2026, 10, 1), amount=-42.0,
                                  description="MYSTERY SHOP", account="a_1")],
    ), settings=Settings(**settings))


def test_the_wrappers_use_the_same_tools_and_respect_the_write_switch(
        tmp_path, monkeypatch):
    _install_fake(monkeypatch)
    from finance_tool.mcp_server import create_server

    store = _store(tmp_path)
    server = create_server(store, today=TODAY)

    assert len(server.tools) == 15
    assert "Accounts:" in server.tools["get_finance_context"]()
    assert "Housing" in server.tools["list_categories"]()
    assert '"count"' in server.tools["query_sql"](
        sql="SELECT id FROM transactions")

    result = server.tools["set_category"](transaction_id="t1", category="Housing")
    assert '"ok": true' in result
    assert store.view().transaction("t1").category == "Housing"
    assert any("before-ai" in path.name for path in (tmp_path / "backups").iterdir())

    accounts = server.resources["finance://accounts"]()
    assert "Everyday" in accounts
    store.close()


def test_the_wrappers_refuse_writes_when_the_switch_is_off(tmp_path, monkeypatch):
    _install_fake(monkeypatch)
    from finance_tool.mcp_server import create_server

    store = _store(tmp_path, ai_writes=False)
    server = create_server(store, today=TODAY)

    result = server.tools["set_category"](transaction_id="t1", category="Housing")

    assert "read-only" in result
    assert store.view().transaction("t1").category is None
    backups = tmp_path / "backups"
    assert not backups.exists() or not list(backups.iterdir())
    store.close()


def test_the_real_sdk_can_build_the_server_when_installed(tmp_path):
    pytest.importorskip("mcp",
                        reason="the MCP SDK surface is an optional extra")
    from finance_tool.mcp_server import create_server

    store = Store(data_dir=tmp_path, doc=StoreDocument(), settings=Settings())
    try:
        assert create_server(store, today=TODAY) is not None
    finally:
        store.close()
