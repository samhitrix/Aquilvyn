"""AI model names: a `.env` comment is never one, and a retired model is replaced from the provider's own list."""
import asyncio

import httpx


def test_an_env_comment_is_never_used_as_an_ai_model_name():
    from advisor_svc.ai.registry import clean, make_client

    assert clean("   # e.g. llama-3.3-70b-versatile") is None and clean("#e.g.llama-3.3-70b-versatile") is None
    assert clean("llama-3.3-70b-versatile  # fast one") == "llama-3.3-70b-versatile" and clean("\tgemini-3-flash\n") == "gemini-3-flash"
    assert make_client("groq", "# e.g. llama-3.3-70b-versatile", "gsk_x", None).model == "auto"  # picked from Groq's list at first use


def test_the_strongest_chat_model_is_picked_from_a_providers_list():
    from advisor_svc.ai.openai_compat import pick_model

    ids = ["whisper-large-v3", "meta-llama/llama-guard-4-12b", "playai-tts", "openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3-32b"]
    assert pick_model(ids) == "openai/gpt-oss-120b"
    assert pick_model(["mystery-8b", "mystery-70b", "whisper-x"]) == "mystery-70b"
    assert pick_model(["whisper-large-v3"]) is None


def test_a_retired_model_is_replaced_and_the_call_still_answers(monkeypatch):
    from advisor_svc.ai import openai_compat as oc

    oc._picked.clear()
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-120b"}, {"id": "whisper-large-v3"}]})
        model = __import__("json").loads(req.content)["model"]
        if model == "llama-3.3-70b-versatile":
            return httpx.Response(404, json={"error": {"message": "The model `llama-3.3-70b-versatile` does not exist or you do not have access to it."}})
        return httpx.Response(200, json={"model": model, "choices": [{"message": {"content": "ok"}}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(oc.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    c = oc.OpenAICompatClient("groq", "llama-3.3-70b-versatile", "https://api.groq.test/openai/v1", "gsk_x")
    assert asyncio.run(c.complete("sys", "hi")) == "ok"
    assert c.model == "openai/gpt-oss-120b" and "no longer available" in c.note
    assert calls == ["/openai/v1/chat/completions", "/openai/v1/models", "/openai/v1/chat/completions"]
