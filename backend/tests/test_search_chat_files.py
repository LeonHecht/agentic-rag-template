from pathlib import Path
import io
from dotenv import load_dotenv
import os
import asyncio
from types import SimpleNamespace

from fastapi import HTTPException
import pandas as pd

# Load the .env file from project root (2 levels up from /backend/tests)
load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env")

import pytest

from backend.app.core.config import settings
from backend.app.api.v1.endpoints import search as search_ep
from backend.app.api.v1.endpoints import chat as chat_ep
from backend.app.api.v1.endpoints import files as files_ep
# from backend.app.api.v1.schemas import ChatRequest   # no longer used
from backend.app.services import auth
from backend.app.services.search import search_engine
from starlette.datastructures import UploadFile


# These tests now target the OpenSearch backend (default in settings).


@pytest.fixture()
def test_env(tmp_path, monkeypatch):
    """Prepare temp dirs, reset databases and index a small document."""
    # Point CORPUS_PATH to a temporary directory with a tiny corpus.jsonl
    corpus_dir = tmp_path / "static_corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    corpus_file = corpus_dir / "corpus.jsonl"
    corpus_file.write_text(
        '{"id": "1", "title": "Product Handbook", "text": "The handbook explains document retrieval workflows."}\n'
        '{"id": "2", "title": "Support Policy", "text": "Priority levels guide support responses."}\n'
        '{"id": "3", "title": "Security Overview", "text": "Security guidance covers secrets and uploads."}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "CORPUS_PATH", str(corpus_dir))

    monkeypatch.setattr(settings, "DATA_UPLOAD", str(tmp_path / "uploads"))
    monkeypatch.setattr(files_ep, "UPLOADS_ROOT", Path(settings.DATA_UPLOAD))
    files_ep.UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)

    # Build a minimal UserData and stub accessible spaces for endpoints
    import uuid as _uuid
    user = auth.UserData(user_id=str(_uuid.uuid4()), username="alice", spaces=["personal"], first_name="Alice", last_name="Test")
    monkeypatch.setattr(search_ep, "get_accessible_spaces", lambda u: [settings.DEFAULT_SPACE, "alice/personal"])
    monkeypatch.setattr(files_ep, "get_accessible_spaces", lambda u: [settings.DEFAULT_SPACE, "alice/personal"])

    # Index the built-in default corpus and a simple personal document
    # For OpenSearch backend, this will create/refresh the alias for the space.
    search_engine.index(settings.DEFAULT_SPACE)

    # Create a simple document in alice's personal space
    uploads_dir = Path(settings.DATA_UPLOAD) / "alice" / "personal"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    (uploads_dir / "doc1.txt").write_text("hello world test document", encoding="utf-8")

    search_engine.index("alice/personal")
    return user


@pytest.fixture()
def fake_openai(monkeypatch):
    """Mock the OpenAI Responses client used by chat_ep.client."""
    class FakeItem:
        def __init__(self, type="message", name=None, arguments=None, call_id=None, summary=None):
            self.type = type
            self.name = name
            self.arguments = arguments
            self.call_id = call_id
            self.summary = summary
        # The endpoint calls sanitize_output_items which expects model_dump() or dict-like
        def model_dump(self):
            # Minimal assistant message; sanitize_output_items will keep role+content
            return {"type": "message", "role": "assistant", "content": "Test answer"}

    class FakeResponse:
        def __init__(self, text="Test answer"):
            self.output = [FakeItem("message")]
            self.output_text = text

    class FakeResponsesAPI:
        def create(self, model, instructions, input, tools, parallel_tool_calls, tool_choice, max_tool_calls, reasoning, stream=None):
            # Return a one-shot assistant message so the loop exits
            return FakeResponse("Test answer")

    class FakeClient:
        def __init__(self):
            self.responses = FakeResponsesAPI()

    monkeypatch.setattr(chat_ep, "client", FakeClient())
    return True


def test_search_basic(test_env):
    user = test_env
    resp = search_ep.search(q="handbook", top_k=3, space=settings.DEFAULT_SPACE, user=user)
    assert resp.results
    hit = resp.results[0]
    assert hit.score > 0


def test_file_upload_creates_file_and_indexes(test_env, monkeypatch):
    user = test_env
    uploaded = UploadFile(filename="new.txt", file=io.BytesIO(b"some content"))
    indexed = []

    def fake_index(space):
        indexed.append(space)

    monkeypatch.setattr(search_engine, "index", fake_index)

    resp = asyncio.run(files_ep.upload_file(files=[uploaded], space="alice/personal", user=user))
    saved = resp["uploaded"][0]["saved_path"]
    path = Path(settings.DATA_UPLOAD) / "alice" / "personal" / saved
    assert path.exists()
    assert indexed == ["alice/personal"]


def test_csv_upload_preserves_existing_analytics_loader_behavior(test_env, monkeypatch):
    user = test_env
    uploaded = UploadFile(filename="metrics.csv", file=io.BytesIO(b"COUNTRY,CITY\nCO,Bogota\n"))
    loaded_paths = []
    indexed = []

    def fake_load_csv_to_duckdb(path):
        loaded_paths.append(Path(path))
        return {"loaded": True, "dataset": "metrics"}

    monkeypatch.setattr(files_ep, "load_csv_to_duckdb", fake_load_csv_to_duckdb)
    monkeypatch.setattr(search_engine, "index", lambda space: indexed.append(space))

    resp = asyncio.run(files_ep.upload_file(files=[uploaded], space="alice/personal", user=user))

    saved_path = Path(settings.DATA_UPLOAD) / "alice" / "personal" / resp["uploaded"][0]["saved_path"]
    assert saved_path.exists()
    assert loaded_paths == [saved_path]
    assert resp["converted"] == []
    assert resp["analytics"] == [{
        "loaded": True,
        "dataset": "metrics",
        "filename": "metrics.csv",
        "saved_path": saved_path.name,
    }]
    assert indexed == ["alice/personal"]


def test_excel_upload_converts_each_sheet_and_loads_generated_csvs(test_env, monkeypatch):
    user = test_env
    workbook = io.BytesIO()
    with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
        pd.DataFrame({"COUNTRY": ["CO"], "CITY": ["Bogota"]}).to_excel(
            writer,
            sheet_name="Metrics 2026!",
            index=False,
        )
        pd.DataFrame({"COUNTRY": ["MX"], "CITY": ["CDMX"]}).to_excel(
            writer,
            sheet_name="Orders Raw #",
            index=False,
        )
    workbook.seek(0)
    uploaded = UploadFile(filename="source.xlsx", file=workbook)
    loaded_paths = []
    indexed = []

    def fake_load_csv_to_duckdb(path):
        loaded_paths.append(Path(path))
        return {"loaded": True}

    monkeypatch.setattr(files_ep, "load_csv_to_duckdb", fake_load_csv_to_duckdb)
    monkeypatch.setattr(search_engine, "index", lambda space: indexed.append(space))

    resp = asyncio.run(files_ep.upload_file(files=[uploaded], space="alice/personal", user=user))

    space_dir = Path(settings.DATA_UPLOAD) / "alice" / "personal"
    assert len(resp["uploaded"]) == 1
    assert len(resp["converted"]) == 2
    assert len(resp["analytics"]) == 2
    assert [item["sheet"] for item in resp["converted"]] == ["Metrics 2026!", "Orders Raw #"]
    assert all(Path(item["saved_path"]).suffix == ".csv" for item in resp["converted"])
    assert all((space_dir / item["saved_path"]).exists() for item in resp["converted"])
    assert all(item["source_workbook"] == "source.xlsx" for item in resp["analytics"])
    assert [path.name for path in loaded_paths] == [item["saved_path"] for item in resp["converted"]]
    converted_names = [item["saved_path"] for item in resp["converted"]]
    assert any(name.endswith("__Metrics_2026.csv") for name in converted_names)
    assert any(name.endswith("__Orders_Raw.csv") for name in converted_names)
    assert indexed == ["alice/personal"]


def test_upload_rejects_mixed_excel_and_other_files(test_env):
    user = test_env
    workbook = UploadFile(filename="source.xlsx", file=io.BytesIO(b"not read"))
    csv = UploadFile(filename="metrics.csv", file=io.BytesIO(b"a,b\n1,2\n"))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(files_ep.upload_file(files=[workbook, csv], space="alice/personal", user=user))

    assert exc_info.value.status_code == 400
    assert "Upload either CSV files or one Excel workbook" in exc_info.value.detail


def test_upload_rejects_multiple_excel_files(test_env):
    user = test_env
    first = UploadFile(filename="first.xlsx", file=io.BytesIO(b"not read"))
    second = UploadFile(filename="second.xlsm", file=io.BytesIO(b"not read"))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(files_ep.upload_file(files=[first, second], space="alice/personal", user=user))

    assert exc_info.value.status_code == 400
    assert "Only one Excel workbook" in exc_info.value.detail


def test_stream_emits_progress_before_tool_execution(monkeypatch):
    class FakeStream:
        def __init__(self, events):
            self.events = events

        def __iter__(self):
            return iter(self.events)

        def close(self):
            pass

    tool_item = SimpleNamespace(
        type="function_call",
        name="emit_event",
        arguments='{"message":"Voy a consultar los datos"}',
        call_id="call_1",
    )

    class FakeResponsesAPI:
        def create(self, **kwargs):
            has_tool_output = any(
                isinstance(item, dict) and item.get("type") == "function_call_output"
                for item in kwargs.get("input", [])
            )
            if has_tool_output:
                return FakeStream([
                    SimpleNamespace(type="response.output_text.delta", delta="Listo"),
                    SimpleNamespace(type="response.output_text.done", text="Listo"),
                    SimpleNamespace(type="response.completed"),
                ])
            return FakeStream([
                SimpleNamespace(type="response.output_item.done", output_index=0, item=tool_item),
                SimpleNamespace(type="response.completed"),
            ])

    fake_client = SimpleNamespace(responses=FakeResponsesAPI())
    monkeypatch.setattr(chat_ep, "get_openai_client", lambda: fake_client)
    monkeypatch.setattr(chat_ep, "run_tool", lambda ctx, tool_name, tool_args: '{"ok": true}')
    monkeypatch.setattr(chat_ep, "get_title_for_chat", lambda last_user_msg: "Test")

    async def collect_stream():
        req = chat_ep.AgenticChatRequest(
            space="personal",
            messages=[{"role": "user", "content": "Analiza ventas"}],
        )
        response = await chat_ep.chat_agentic_stream(req)
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode("utf-8"))
        return "".join(chunks)

    body = asyncio.run(collect_stream())

    assert "event: response.emit_message" in body
    assert "Voy a consultar los datos" in body
    assert body.index("Voy a consultar los datos") < body.index("Listo")


def test_stream_converts_plain_emit_event_json_to_progress(monkeypatch):
    class FakeStream:
        def __init__(self, events):
            self.events = events

        def __iter__(self):
            return iter(self.events)

        def close(self):
            pass

    calls = {"count": 0}

    class FakeResponsesAPI:
        def create(self, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                text = '{"kind":"decision","message":"Revisaré el esquema"}'
                return FakeStream([
                    SimpleNamespace(type="response.output_text.delta", delta=text[:20]),
                    SimpleNamespace(type="response.output_text.delta", delta=text[20:]),
                    SimpleNamespace(type="response.output_text.done", text=text),
                    SimpleNamespace(type="response.completed"),
                ])
            return FakeStream([
                SimpleNamespace(type="response.output_text.delta", delta="El promedio es 42%."),
                SimpleNamespace(type="response.output_text.done", text="El promedio es 42%."),
                SimpleNamespace(type="response.completed"),
            ])

    fake_client = SimpleNamespace(responses=FakeResponsesAPI())
    monkeypatch.setattr(chat_ep, "get_openai_client", lambda: fake_client)
    monkeypatch.setattr(chat_ep, "get_title_for_chat", lambda last_user_msg: "Test")

    async def collect_stream():
        req = chat_ep.AgenticChatRequest(
            space="personal",
            messages=[{"role": "user", "content": "Promedio por país"}],
        )
        response = await chat_ep.chat_agentic_stream(req)
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode("utf-8"))
        return "".join(chunks)

    body = asyncio.run(collect_stream())

    assert "event: response.emit_message" in body
    assert "Revisaré el esquema" in body
    assert "event: response.output_text.delta" in body
    assert "El promedio es 42%." in body
    assert '{"kind":"decision"' not in body


def test_stream_emits_progress_when_tool_call_item_is_added(monkeypatch):
    class FakeStream:
        def __init__(self, events):
            self.events = events

        def __iter__(self):
            return iter(self.events)

        def close(self):
            pass

    tool_item = SimpleNamespace(
        type="function_call",
        name="run_sql",
        arguments="",
        call_id="call_1",
    )

    class FakeResponsesAPI:
        def create(self, **kwargs):
            has_tool_output = any(
                isinstance(item, dict) and item.get("type") == "function_call_output"
                for item in kwargs.get("input", [])
            )
            if has_tool_output:
                return FakeStream([
                    SimpleNamespace(type="response.output_text.delta", delta="Resultado final"),
                    SimpleNamespace(type="response.output_text.done", text="Resultado final"),
                    SimpleNamespace(type="response.completed"),
                ])
            return FakeStream([
                SimpleNamespace(type="response.output_item.added", output_index=0, item=tool_item),
                SimpleNamespace(type="response.function_call_arguments.delta", output_index=0, delta='{"sql":"select 1"'),
                SimpleNamespace(type="response.function_call_arguments.delta", output_index=0, delta="}"),
                SimpleNamespace(type="response.function_call_arguments.done", output_index=0, arguments='{"sql":"select 1"}'),
                SimpleNamespace(type="response.output_item.done", output_index=0, item=tool_item),
                SimpleNamespace(type="response.completed"),
            ])

    fake_client = SimpleNamespace(responses=FakeResponsesAPI())
    monkeypatch.setattr(chat_ep, "get_openai_client", lambda: fake_client)
    monkeypatch.setattr(chat_ep, "run_tool", lambda ctx, tool_name, tool_args: '{"rows": [{"x": 1}]}')
    monkeypatch.setattr(chat_ep, "get_title_for_chat", lambda last_user_msg: "Test")

    async def collect_stream():
        req = chat_ep.AgenticChatRequest(
            space="personal",
            messages=[{"role": "user", "content": "Consulta datos"}],
        )
        response = await chat_ep.chat_agentic_stream(req)
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode("utf-8"))
        return "".join(chunks)

    body = asyncio.run(collect_stream())

    assert "event: response.emit_message" in body
    assert "Ejecutando una consulta SQL analítica" in body
    assert body.count("Ejecutando una consulta SQL analítica") == 1
    assert body.index("Ejecutando una consulta SQL analítica") < body.index("Resultado final")
