import httpx
import json
import pytest

from murasamebot.adapters.models.chat_completions import ChatCompletionsModel
from murasamebot.domain.models import ModelError


def test_full_expression_catalog_keeps_kaomoji_after_native_emotes(factory):
    from murasamebot.application.expressions import KAOMOJIS
    _,store,_,_=factory()
    options=[{"key":f"bilibili:{i}","token":f"[千恋万花_{i}]","kind":"native"} for i in range(24)]
    options.extend({"key":e.key,"token":e.token,"kind":e.kind} for e in KAOMOJIS)
    def handle(request):
        payload=json.loads(json.loads(request.content)["messages"][1]["content"])
        assert len(payload["emoji_options"])==32
        assert payload["emoji_options"][-1]["key"]==KAOMOJIS[-1].key
        return httpx.Response(200,json={"choices":[{"message":{"content":'{"decision":"reply","text":"吾辈明白啦。"}'}}]})
    gateway=ChatCompletionsModel(base_url="https://model.invalid",name="fake",api_key="",store=store,
        timezone="Asia/Hong_Kong",daily_limit=1,client=httpx.Client(transport=httpx.MockTransport(handle)))
    try:
        gateway.decide("reply",{"emoji_options":options})
    finally:
        gateway.close()


def test_compatible_gateway_parses_json_and_records_usage(factory):
    engine, store, model, platform = factory()
    def handle(request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer private-key"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"decision":"reply","text":"吾辈明白了。"}'}}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
    gateway = ChatCompletionsModel(base_url="https://model.invalid/v1", name="configured-model", api_key="private-key", store=store,
                                   timezone="Asia/Hong_Kong", daily_limit=2, client=httpx.Client(transport=httpx.MockTransport(handle)))
    result = gateway.decide("reply", {"message": "你好"})
    assert result["text"] == "吾辈明白了。"
    assert store.inspect("model_usage")[0]["tokens_in"] == 100


def test_malformed_model_output_is_not_used_as_a_comment(factory):
    engine, store, model, platform = factory()
    gateway = ChatCompletionsModel(base_url="https://model.invalid/v1", name="model", api_key="secret", store=store,
                                   timezone="Asia/Hong_Kong", daily_limit=2,
                                   client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "plaintext-not-json"}}]}))))
    with pytest.raises(ModelError) as exc:
        gateway.decide("reply", {"message": "private-message"})
    assert "private-message" not in str(exc.value) and "secret" not in str(exc.value)


@pytest.mark.parametrize("limit", [None, 8192])
def test_output_limit_uses_provider_default_unless_explicitly_configured(factory, limit):
    engine, store, model, platform = factory()
    def handle(request):
        body = json.loads(request.content)
        if limit is None:
            assert "max_tokens" not in body
        else:
            assert body["max_tokens"] == limit
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"decision":"reply","text":"你好。"}'}}]})
    gateway = ChatCompletionsModel(base_url="https://model.invalid", name="model", api_key="secret", store=store,
                                   timezone="Asia/Hong_Kong", daily_limit=2, max_output_tokens=limit,
                                   client=httpx.Client(transport=httpx.MockTransport(handle)))
    assert gateway.decide("reply", {"message": "你好"})["decision"] == "reply"


@pytest.mark.parametrize("finish,content,expected", [
    ("length", '{"decision":', "output_truncated"),
    ("stop", "", "empty_content"),
    ("stop", None, "empty_content"),
    ("stop", "not-json", "invalid_content_json"),
])
def test_model_failures_are_classified_without_logging_private_content(factory, finish, content, expected):
    engine, store, model, platform = factory()
    response = {"choices": [{"finish_reason": finish, "message": {"content": content, "reasoning_content": "private-reasoning"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 700, "completion_tokens_details": {"reasoning_tokens": 690}}}
    gateway = ChatCompletionsModel(base_url="https://model.invalid", name="model", api_key="secret-token", store=store,
                                   timezone="Asia/Hong_Kong", daily_limit=2,
                                   client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response))))
    with pytest.raises(ModelError):
        gateway.decide("reply", {"message": "private-source-message"})
    diagnostic = json.loads(next(row["detail"] for row in store.inspect("audit") if row["kind"] == "model_response_error"))
    assert diagnostic["error"] == expected and diagnostic["finish_reason"] == finish
    assert diagnostic["reasoning_tokens"] == 690
    assert all(secret not in str(diagnostic) for secret in ("private-source-message", "private-reasoning", "secret-token"))
    assert store.inspect("model_usage")[0]["tokens_out"] == 700
