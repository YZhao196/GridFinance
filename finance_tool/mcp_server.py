"""MCP server so Claude Desktop / Claude Code can query the ledger directly (§24).

Run:  python -m finance_tool.mcp_server
Register it in Claude Desktop's config as a stdio server with that command.

The server needs the optional ``mcp`` extra. Writes go through the same tools and the
same ``ai_writes`` switch as the in-app assistant, and are saved immediately because an
MCP client cannot see the app's in-memory store.
"""

from __future__ import annotations

import json
from datetime import date

from finance_tool import query as sqlquery
from finance_tool.assistant.tools import WRITE_TOOLS, execute_tool
from finance_tool.store.store import Store


def create_server(store: Store | None = None, *, today: date | None = None,
                  data_dir=None):
    try:
        # mcp 1.x ships FastMCP; the 2.x line moved it to mcp.server.mcpserver.
        from mcp.server.fastmcp import FastMCP as MCPServer
    except ImportError:
        try:
            from mcp.server.mcpserver import MCPServer
        except ImportError as exc:  # pragma: no cover - depends on the optional extra
            raise ImportError(
                "the MCP server needs the 'mcp' extra: pip install gridfinance[mcp]"
            ) from exc

    # A Store is bound to the thread that opened its SQLite connection, and mcp 2.x runs
    # handlers on worker threads. With no injected store, each call opens and closes its
    # own — the DB is the shared state.
    active = store
    day = today or date.today()
    server = MCPServer("gridfinance")

    def run(tool: str, **arguments) -> str:
        write = tool in WRITE_TOOLS
        target = active if active is not None else Store.load(data_dir, create=write)
        try:
            if (write and target.settings.ai_writes
                    and target.data_dir is not None):
                try:
                    target.revision("before-ai")
                except Exception:
                    pass             # a scratch or read-only store has no history
            result = execute_tool(target, tool, arguments, today=day)
            if write:
                try:
                    payload = json.loads(result)
                except ValueError:
                    payload = {}
                if payload.get("ok"):
                    target.save()
            return result
        except Exception as exc:
            return json.dumps({"error": str(exc)})
        finally:
            if active is None:
                target.close()

    @server.tool()
    def get_finance_context() -> str:
        """Balances, recent spending, top merchants and outstanding review items."""
        return run("get_finance_context")

    @server.tool()
    def search_transactions(query: str | None = None, start: str | None = None,
                            end: str | None = None, category: str | None = None,
                            account: str | None = None, limit: int = 50) -> str:
        """Find transactions. Dates are ISO YYYY-MM-DD. Returns JSON."""
        arguments = {key: value for key, value in {
            "query": query, "start": start, "end": end,
            "category": category, "account": account}.items() if value}
        arguments["limit"] = limit
        return run("search_transactions", **arguments)

    @server.tool()
    def get_spending_summary(start: str | None = None, end: str | None = None) -> str:
        """Totals by category, merchant and month for a date range."""
        return run("get_spending_summary", start=start, end=end)

    @server.tool()
    def list_uncategorised(limit: int = 40) -> str:
        """Transactions that have no category yet."""
        return run("list_uncategorised", limit=limit)

    @server.tool()
    def list_review_items() -> str:
        """Questions waiting for the user about ambiguous transactions."""
        return run("list_review_items")

    @server.tool()
    def list_categories() -> str:
        """All available category names."""
        return run("list_categories")

    @server.tool()
    def set_category(transaction_id: str, category: str) -> str:
        """Assign a category to one transaction."""
        return run("set_category", transaction_id=transaction_id, category=category)

    @server.tool()
    def create_rule(pattern: str, category: str, match_type: str = "contains") -> str:
        """Create a categorisation rule; also applies it to matching history."""
        return run("create_rule", pattern=pattern, category=category,
                   match_type=match_type)

    @server.tool()
    def add_note(transaction_id: str, note: str) -> str:
        """Attach a note to a transaction."""
        return run("add_note", transaction_id=transaction_id, note=note)

    @server.tool()
    def query_sql(sql: str, limit: int = 200) -> str:
        """Run a read-only SQL SELECT against the ledger. Schema:\n\n""" + sqlquery.SCHEMA_DOC
        return run("query_sql", sql=sql, limit=limit)

    @server.tool()
    def list_recurring() -> str:
        """Detected subscriptions and recurring payments."""
        return run("list_recurring")

    @server.tool()
    def upcoming_expenses(days: int = 30) -> str:
        """Recurring payments due within the next N days."""
        return run("upcoming_expenses", days=days)

    @server.tool()
    def write_summaries() -> str:
        """Write the markdown summary for each financial year that has transactions."""
        return run("write_summaries")

    @server.tool()
    def read_profile() -> str:
        """The user's life/finance profile notes."""
        return run("read_profile")

    @server.tool()
    def append_to_profile(text: str) -> str:
        """Add a durable fact about the user's finances to their profile."""
        return run("append_to_profile", text=text)

    @server.resource("finance://context")
    def context_resource() -> str:
        return run("get_finance_context")

    @server.resource("finance://accounts")
    def accounts_resource() -> str:
        target = active if active is not None else Store.load(data_dir, create=False)
        try:
            return json.dumps([{
                "id": account.id,
                "name": account.name,
                "kind": account.kind,
                "currency": account.currency,
                "balance": account.balance,
            } for account in target.view().accounts], indent=2)
        finally:
            if active is None:
                target.close()

    return server


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
