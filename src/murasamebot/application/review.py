"""Same-generation publication judgement; never changes recipients or permissions."""
from dataclasses import replace

from murasamebot.domain.models import Action


def inline_review(action: Action, result: dict) -> tuple[Action, bool]:
    """Bind a typed verdict to the actual rendered text; missing verdicts need a human."""
    allowed = result.get("can_send")
    reason = result.get("review_reason")
    state = "unavailable"
    explanation = "同次可发送判断缺失或格式无效，等待人工审核"
    if type(allowed) is bool and isinstance(reason,str) and 0<len(reason.strip())<=240:
        state = "passed" if allowed else "rejected"
        explanation = reason.strip()
        raw = result.get("text")
        keys = result.get("emojis",[])
        if (not isinstance(raw,str) or not isinstance(keys,list) or keys!=[e.key for e in action.emojis]
                or raw.strip()+"".join(e.token for e in action.emojis)!=action.text):
            state = "unavailable"
            explanation = "实际正文或表情与生成判断不一致，等待人工审核"
    return replace(action,review_state=state,review_reason=explanation,review_method="inline"),state!="passed"


def review_satisfied(action: Action, strategy: str) -> bool:
    return action.review_state=="passed" and not (strategy=="independent" and action.review_method=="inline")
