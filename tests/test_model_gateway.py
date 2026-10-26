import httpx
import pytest

from murasamebot.adapters.models.chat_completions import ChatCompletionsModel
from murasamebot.domain.models import ModelError


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
