"""The entities and collections Stage 0 adds must survive a real save and reload."""

from datetime import date

from finance_tool.store.entities import (
    Account,
    ChatMessage,
    ImportRecord,
    ReviewItem,
    Rule,
    Settings,
    StoreDocument,
    Transaction,
)
from finance_tool.store.store import Store


def test_the_new_collections_round_trip(tmp_path):
    store = Store(data_dir=tmp_path, doc=StoreDocument(), settings=Settings())
    store.add("reviews", ReviewItem(
        id="rv_1", transaction_id="t_1", options=["Groceries", "Dining"],
        suggested_category="Groceries", suggested_confidence=0.42))
    store.add("imports", ImportRecord(
        id="im_1", filename="September.pdf", rows_new=3,
        span_start=date(2026, 9, 1), span_end=date(2026, 9, 30)))
    store.add("messages", ChatMessage(
        id="cm_1", role="user", content="where did my money go?",
        tool_calls=[{"name": "get_spending_summary", "arguments": {}}]))
    store.save()
    store.close()

    again = Store.load(tmp_path)

    assert again.doc.reviews[0].options == ["Groceries", "Dining"]
    assert again.doc.reviews[0].suggested_confidence == 0.42
    assert again.doc.imports[0].filename == "September.pdf"
    assert again.doc.imports[0].span_start == date(2026, 9, 1)
    assert again.doc.messages[0].tool_calls[0]["name"] == "get_spending_summary"
    again.close()


def test_transaction_provenance_round_trips(tmp_path):
    store = Store(data_dir=tmp_path, doc=StoreDocument(), settings=Settings())
    store.add("transactions", Transaction(
        id="t_1", date=date(2026, 10, 3), amount=-50.0, description="coles",
        merchant="Coles", category="Groceries", category_source="llm",
        category_confidence=0.82, needs_review=True, note="weekly shop",
        status="pending", value_date=date(2026, 10, 4)))
    store.save()
    store.close()

    again = Store.load(tmp_path)
    row = again.doc.transactions[0]

    assert row.merchant == "Coles"
    assert row.category_source == "llm"
    assert row.category_confidence == 0.82
    assert row.needs_review is True
    assert row.note == "weekly shop"
    assert row.status == "pending"
    assert row.value_date == date(2026, 10, 4)
    again.close()


def test_rule_and_account_fields_round_trip(tmp_path):
    store = Store(data_dir=tmp_path, doc=StoreDocument(), settings=Settings())
    store.add("accounts", Account(
        id="a_1", name="Everyday", balance=1200.0, external_id="ext-1",
        provider="redbark"))
    store.add("rules", Rule(
        id="r_1", match="COLES", category="Groceries", name="Coles",
        match_type="contains", amount_min=5.0, amount_max=200.0,
        active=False, hit_count=12))
    store.save()
    store.close()

    again = Store.load(tmp_path)

    assert again.doc.accounts[0].provider == "redbark"
    rule = again.doc.rules[0]
    assert (rule.name, rule.match_type, rule.amount_min, rule.hit_count) == \
        ("Coles", "contains", 5.0, 12)
    assert rule.active is False
    again.close()


def test_settings_gain_their_new_defaults_and_persist(tmp_path):
    store = Store.load(tmp_path)
    assert store.settings.ai_writes is True
    assert store.settings.review_threshold == 0.6
    assert store.settings.feed_provider == ""

    store.settings.ai_provider = "anthropic"
    store.settings.feed_provider = "redbark"
    store.settings.ai_writes = False
    store.settings.onboarded = True
    store.save_settings()
    store.close()

    again = Store.load(tmp_path)
    assert again.settings.ai_provider == "anthropic"
    assert again.settings.feed_provider == "redbark"
    assert again.settings.ai_writes is False
    assert again.settings.onboarded is True
    again.close()
