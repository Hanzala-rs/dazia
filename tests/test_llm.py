import json

import httpx
import pytest

from app.llm import LLMError, LLMUnavailable, OllamaClient

SCHEMA = {"type": "object", "properties": {"tool": {"type": "string"}}, "required": ["tool"]}


def make_client(handler, model="qwen3.5:4b"):
    return OllamaClient("http://ollama.test", model, timeout_s=5, transport=httpx.MockTransport(handler))


def chat_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}, "done": True})


def test_complete_json_success_and_payload_shape():
    seen = {}

    def handler(request: httpx.Request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return chat_response('{"tool": "none"}')

    client = make_client(handler)
    msgs = [{"role": "user", "content": "hi"}]
    assert client.complete_json(msgs, SCHEMA) == {"tool": "none"}
    body = seen["body"]
    assert seen["path"] == "/api/chat"
    assert body["model"] == "qwen3.5:4b"
    assert body["messages"] == msgs
    assert body["stream"] is False
    assert body["think"] is False
    assert body["format"] == SCHEMA
    assert body["options"] == {"temperature": 0, "num_ctx": 2048}


def test_bad_json_retries_once_then_raises():
    calls = []

    def handler(request):
        calls.append(1)
        return chat_response("not json {")

    with pytest.raises(LLMError) as exc:
        make_client(handler).complete_json([], SCHEMA)
    assert not isinstance(exc.value, LLMUnavailable)
    assert len(calls) == 2  # one retry, bounded


def test_bad_json_then_good_json_recovers():
    replies = iter(["oops", '{"tool": "list_reminders"}'])

    def handler(request):
        return chat_response(next(replies))

    assert make_client(handler).complete_json([], SCHEMA) == {"tool": "list_reminders"}


def test_non_object_json_is_rejected():
    with pytest.raises(LLMError):
        make_client(lambda r: chat_response("[1, 2]")).complete_json([], SCHEMA)


def test_connection_refused_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LLMUnavailable) as exc:
        make_client(handler).complete_json([], SCHEMA)
    assert "Ollama" in str(exc.value)


def test_timeout_is_llm_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMError) as exc:
        make_client(handler).complete_json([], SCHEMA)
    assert not isinstance(exc.value, LLMUnavailable)


def test_model_not_found_is_unavailable_with_pull_hint():
    def handler(request):
        return httpx.Response(404, json={"error": "model 'qwen3.5:4b' not found"})

    with pytest.raises(LLMUnavailable) as exc:
        make_client(handler).complete_json([], SCHEMA)
    assert "ollama pull qwen3.5:4b" in str(exc.value)


def test_server_error_is_llm_error():
    with pytest.raises(LLMError) as exc:
        make_client(lambda r: httpx.Response(500, json={"error": "boom"})).complete_json([], SCHEMA)
    assert not isinstance(exc.value, LLMUnavailable)


def tags_handler(names):
    def handler(request):
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": n, "model": n} for n in names]})

    return handler


def test_health_ok_when_model_installed():
    ok, detail = make_client(tags_handler(["llama3.1:8b", "qwen3.5:4b"])).health()
    assert ok is True


def test_health_reports_missing_model():
    ok, detail = make_client(tags_handler(["llama3.1:8b"])).health()
    assert ok is False
    assert "ollama pull qwen3.5:4b" in detail


def test_health_never_raises_when_down():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    ok, detail = make_client(handler).health()
    assert ok is False
    assert "Ollama" in detail
