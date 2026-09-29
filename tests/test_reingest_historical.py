import mongomock
import pytest

from app import replay_sample
from app.interfaces.llm_provider import LLMProvider
from scripts import reingest_historical


def _email_doc(message_id="MSG-1", timestamp="2026-08-01T09:00:00Z", **overrides):
    doc = {
        "message_id": message_id,
        "from": {"name": "Jane", "email": "jane@customerco.example"},
        "to": [{"name": "Ashok", "email": "ashok@ourcompany.example"}],
        "cc": [],
        "subject": "Renewal pricing",
        "body": "Can we discuss renewal pricing for next quarter?",
        "timestamp": timestamp,
        "processing_status": {"stage": "COMPLETED", "error": None},
        "entities_referenced": {"people": [], "projects": [], "commitments": [], "meetings": []},
    }
    doc.update(overrides)
    return doc


# --- _ReadOnlyCollectionProxy / _ReadOnlyDatabaseProxy: the actual enforcement ------


@pytest.fixture
def source_collection():
    client = mongomock.MongoClient()
    return client["some_db"]["emails"]


@pytest.mark.parametrize(
    "method_name,args",
    [
        ("insert_one", ({"x": 1},)),
        ("insert_many", ([{"x": 1}],)),
        ("update_one", ({}, {"$set": {"x": 1}})),
        ("update_many", ({}, {"$set": {"x": 1}})),
        ("delete_one", ({},)),
        ("delete_many", ({},)),
        ("drop", ()),
        ("create_index", ("message_id",)),
        ("bulk_write", ([],)),
        ("find_one_and_update", ({}, {"$set": {"x": 1}})),
        ("find_one_and_delete", ({},)),
        ("rename", ("new_name",)),
    ],
)
def test_read_only_collection_proxy_blocks_every_write_method(source_collection, method_name, args):
    proxy = reingest_historical._ReadOnlyCollectionProxy(source_collection)

    with pytest.raises(RuntimeError, match=f"write method \\({method_name!r}\\)"):
        getattr(proxy, method_name)(*args)

    # The underlying collection was never actually touched.
    assert source_collection.count_documents({}) == 0


def test_read_only_collection_proxy_allows_read_methods(source_collection):
    source_collection.insert_one({"message_id": "MSG-1"})
    proxy = reingest_historical._ReadOnlyCollectionProxy(source_collection)

    assert proxy.count_documents({}) == 1
    assert list(proxy.find({}, {"_id": 0})) == [{"message_id": "MSG-1"}]


def test_read_only_database_proxy_wraps_both_bracket_and_attribute_access():
    client = mongomock.MongoClient()
    database = client["some_db"]
    proxy = reingest_historical._ReadOnlyDatabaseProxy(database)

    assert isinstance(proxy["emails"], reingest_historical._ReadOnlyCollectionProxy)
    assert isinstance(proxy.emails, reingest_historical._ReadOnlyCollectionProxy)
    with pytest.raises(RuntimeError):
        proxy["emails"].insert_one({"x": 1})
    with pytest.raises(RuntimeError):
        proxy.emails.drop()


def test_read_only_database_proxy_allows_real_read_only_database_methods():
    client = mongomock.MongoClient()
    database = client["some_db"]
    database["emails"].insert_one({"message_id": "MSG-1"})
    proxy = reingest_historical._ReadOnlyDatabaseProxy(database)

    assert "emails" in proxy.list_collection_names()
    assert proxy.name == "some_db"


@pytest.mark.parametrize("method_name", ["drop_collection", "create_collection", "command"])
def test_read_only_database_proxy_blocks_database_level_write_methods(method_name):
    client = mongomock.MongoClient()
    database = client["some_db"]
    proxy = reingest_historical._ReadOnlyDatabaseProxy(database)

    with pytest.raises(RuntimeError, match=f"write/admin method \\({method_name!r}\\)"):
        getattr(proxy, method_name)("emails")


# --- load_source_emails --------------------------------------------------------------


def test_load_source_emails_with_limit_returns_most_recent_first():
    client = mongomock.MongoClient()
    real_db = client["source_db"]
    real_db["emails"].insert_many(
        [
            _email_doc("MSG-OLD", timestamp="2026-01-01T09:00:00Z"),
            _email_doc("MSG-NEW", timestamp="2026-08-01T09:00:00Z"),
        ]
    )
    proxy = reingest_historical._ReadOnlyDatabaseProxy(real_db)

    emails = reingest_historical.load_source_emails(proxy, limit=1, message_ids=None)

    assert [e["message_id"] for e in emails] == ["MSG-NEW"]
    assert "_id" not in emails[0]


def test_load_source_emails_with_message_ids_ignores_limit():
    client = mongomock.MongoClient()
    real_db = client["source_db"]
    real_db["emails"].insert_many(
        [_email_doc("MSG-A"), _email_doc("MSG-B"), _email_doc("MSG-C")]
    )
    proxy = reingest_historical._ReadOnlyDatabaseProxy(real_db)

    emails = reingest_historical.load_source_emails(proxy, limit=None, message_ids=["MSG-A", "MSG-C"])

    assert {e["message_id"] for e in emails} == {"MSG-A", "MSG-C"}


# --- main(): CLI-level guards ---------------------------------------------------------


def test_main_refuses_when_target_db_looks_like_production():
    exit_code = reingest_historical.main(
        [
            "--source-uri", "mongodb://source", "--source-db", "cos_sales_production_v1",
            "--target-uri", "mongodb://target", "--target-db", "cos_sales_production_v1_copy",
            "--limit", "5", "--yes",
        ]
    )
    assert exit_code == 1


def test_main_refuses_when_source_and_target_are_identical():
    exit_code = reingest_historical.main(
        [
            "--source-uri", "mongodb://same", "--source-db", "same_db",
            "--target-uri", "mongodb://same", "--target-db", "same_db",
            "--limit", "5", "--yes",
        ]
    )
    assert exit_code == 1


def test_main_refuses_without_limit_or_message_ids():
    exit_code = reingest_historical.main(
        [
            "--source-uri", "mongodb://source", "--source-db", "cos_sales_production_v1",
            "--target-uri", "mongodb://target", "--target-db", "cos_sales_dryrun",
            "--yes",
        ]
    )
    assert exit_code == 1


def test_main_refuses_without_yes(monkeypatch):
    fake_client = mongomock.MongoClient()
    monkeypatch.setattr(reingest_historical, "get_client", lambda uri: fake_client)
    fake_client["cos_sales_production_v1"]["emails"].insert_one(_email_doc())

    exit_code = reingest_historical.main(
        [
            "--source-uri", "mongodb://source", "--source-db", "cos_sales_production_v1",
            "--target-uri", "mongodb://target", "--target-db", "cos_sales_dryrun",
            "--limit", "5",
        ]
    )

    assert exit_code == 1
    assert fake_client["cos_sales_dryrun"]["emails"].count_documents({}) == 0


class _SalesProjectLLM(LLMProvider):
    def analyze_email(self, email, thread_history=None):
        return {
            "email_id": email.message_id, "summary": email.body[:200], "intent": "buying_signal",
            "entities": [], "facts": [], "requirements": [], "pain_points": [],
            "buying_signals": ["pricing request"], "objections": [], "competitors": [], "pricing_mentions": [],
            "commitments": [], "action_items": [], "meetings": [], "people": [],
            "companies": [], "products": [],
            "people_mentioned": [
                {"name": "Jane", "email": "jane@customerco.example", "org": "CustomerCo", "role_hint": None}
            ],
            "projects_mentioned": [{"name": "CustomerCo Renewal", "org": "CustomerCo", "objective_hint": "Q4 renewal"}],
            "commitments_mentioned": [
                {"what": "send updated MSA", "class": "mine", "owed_by": None, "owed_to": None, "date_phrase": None, "importance_hint": None}
            ],
            "meetings_mentioned": [], "personal_items_mentioned": [],
            "goal_pillar": "Sales", "label_applied": "Needs reply", "priority": "P1", "confidence": 0.9,
        }

    def update_context(self, previous_context, new_analysis):
        return {}

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        return False

    def draft_reply(self, context, latest_email):
        return {"subject": f"Re: {latest_email.subject}", "body": "noop"}


def test_main_reingests_from_source_into_target_without_writing_to_source(monkeypatch, capsys):
    fake_client = mongomock.MongoClient()
    monkeypatch.setattr(reingest_historical, "get_client", lambda uri: fake_client)
    monkeypatch.setattr(
        replay_sample.ProviderFactory, "create_llm_provider", staticmethod(lambda settings: _SalesProjectLLM())
    )
    fake_client["cos_sales_production_v1"]["emails"].insert_one(_email_doc("MSG-REAL"))

    exit_code = reingest_historical.main(
        [
            "--source-uri", "mongodb://source", "--source-db", "cos_sales_production_v1",
            "--target-uri", "mongodb://target", "--target-db", "cos_sales_dryrun",
            "--limit", "5", "--yes",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "completed=1" in output
    assert "goal_pillar='Sales'" in output

    # Source is completely untouched -- still exactly the one seeded document.
    source_docs = list(fake_client["cos_sales_production_v1"]["emails"].find({}))
    assert len(source_docs) == 1
    assert source_docs[0]["message_id"] == "MSG-REAL"

    # Target actually received the derived entities.
    target_projects = list(fake_client["cos_sales_dryrun"]["projects"].find({}))
    assert len(target_projects) == 1
    assert target_projects[0]["goal_pillar"] == "Sales"
    target_commitments = list(fake_client["cos_sales_dryrun"]["commitments"].find({}))
    assert len(target_commitments) == 1
