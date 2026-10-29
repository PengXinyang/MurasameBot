"""Mandatory public-interaction policy; private chat is a separate channel."""
import re
import unicodedata


SENSITIVE = re.compile(r"政治|时政|政党|大选|选举|总统|国家主席|政府|台海|两岸关系|国际局势|地缘|俄乌|巴以|以巴|军事冲突|宗教冲突|种族仇恨|politic|election|geopolitic", re.I)
SERIOUS = re.compile(r"讣告|悼念|遇难|逝世|自杀|自残|确诊|癌症|抑郁症|灾难|事故调查|法律咨询|投资建议|医疗建议|刑事案件|救灾")
CLASSES = {"casual", "serious", "sensitive", "unknown"}


def local_topic(*values):
    text = unicodedata.normalize("NFKC", "\n".join(str(v or "") for v in values))
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text)
    if SENSITIVE.search(text):
        return "sensitive"
    if SERIOUS.search(text):
        return "serious"
    return "casual"  # No lexical hit; model classification is still required.


def combined_topic(local, model):
    if local == "sensitive" or model == "sensitive":
        return "sensitive"
    if not isinstance(model,str) or model not in CLASSES or model == "unknown":
        return "unknown"
    return "serious" if "serious" in {local,model} else "casual"


def blocked(topic, owner=False):
    return topic in {"sensitive","unknown"} or topic == "serious" and not owner


def document_topic(document, message=""):
    if document is None:
        return local_topic(message)
    local = local_topic(document.title,document.text,message,
                        document.visual.summary if document.visual and document.visual.indices else "",
                        *(c.get("text","") for c in document.comments))
    return combined_topic(local,document.topic_class) if document.topic_class in {"sensitive","serious"} else local
