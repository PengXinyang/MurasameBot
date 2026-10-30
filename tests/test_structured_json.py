import json

import httpx
import pytest

from murasamebot.adapters.models.chat_completions import ChatCompletionsModel
from murasamebot.adapters.models.structured_json import parse_object
from murasamebot.domain.models import ModelError


@pytest.mark.parametrize("raw", [
    '{"summary":"图中可见人物", "images":[],}',
    '{"summary":"第一行\n第二行", "images":[]}',
    '```json\n{"summary":"图中可见人物", "images":[],}\n```',
])
def test_visual_syntax_repairs_are_bounded_and_auditable(factory, raw):
    _, store, _, _ = factory()
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": raw}}]})

    model = ChatCompletionsModel(base_url="https://model.invalid", name="vision", api_key="", store=store,
                                timezone="Asia/Hong_Kong", daily_limit=3,
                                client=httpx.Client(transport=httpx.MockTransport(handle)))
    result = model.decide("describe_images", {})
    assert result["summary"] and len(calls) == 1
    records = store.inspect("audit")
    assert any(row["kind"] == "model_response_normalized" for row in records)
    assert not any(row["kind"] == "model_response_error" for row in records)
    assert "图中可见人物" not in str(records) and "第一行" not in str(records)


def test_repairs_do_not_change_quoted_punctuation_or_escaped_strings():
    summary = '看见文字 ,} ,] "引号" 与路径 \\n'
    raw = json.dumps({"summary": summary, "images": []}, ensure_ascii=False)
    result, repairs = parse_object(raw[:-1] + ",}", visual_repairs=True)
    assert result["summary"] == summary and repairs == ("trailing_comma",)


@pytest.mark.parametrize("raw", [
    '{"summary":"未闭合', '{"summary":"无引号"内容"}',
    '{"summary":"第一份"} {"summary":"第二份"}',
    '解释文字 {"summary":"内容"}', '{"summary":NaN}',
    '{"summary":"第一份","summary":"第二份"}',
    '{"summary":undefined}', '{"summary":"非法\\x转义"}',
])
def test_ambiguous_or_incomplete_visual_json_stays_rejected(raw):
    with pytest.raises(ValueError):
        parse_object(raw, visual_repairs=True)


@pytest.mark.parametrize("operation", ["reply", "review_action", "private_chat"])
def test_visual_repair_does_not_repair_publication_or_review_decisions(factory, operation):
    _, store, _, _ = factory()
    raw = '{"decision":"approve","can_send":true,}'
    model = ChatCompletionsModel(base_url="https://model.invalid", name="fake", api_key="", store=store,
                                timezone="Asia/Hong_Kong", daily_limit=3,
                                client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
                                    "choices": [{"finish_reason": "stop", "message": {"content": raw}}]}))))
    with pytest.raises(ModelError):
        model.decide(operation, {})
    diag = json.loads(next(r["detail"] for r in store.inspect("audit") if r["kind"] == "model_response_error"))
    assert diag["error"] == "invalid_content_json" and "json_error_reason" in diag
    assert "json_error_column" in diag and "can_send" not in str(diag)
