import re
import unicodedata


VIDEO_INTEREST_KEYWORDS = (
    "千恋万花", "千恋＊万花", "galgame", "gal game", "美少女游戏", "美少女遊戲",
    "柚子社", "ゆずソフト", "yuzusoft", "Key社", "键社", "key",
    "TYPE-MOON", "typemoon", "型月", "SAGA PLANETS", "SMEE", "ASa Project",
    "Lump of Sugar", "LumpofSugar", "调色板社", "ぱれっと", "Palette",
    "Purple software", "Navel", "August", "八月社", "オーガスト", "Frontwing", "枕社", "ケロQ", "KeroQ",
    "まどそふと", "Qruppo", "HARUKAZE", "sprite", "tone work's",
    "FAVORITE", "ういんどみる", "Nitroplus", "ニトロプラス", "AQUAPLUS", "戯画", "戏画社",
)


def interest_exempt(document, keywords):
    # Titles/tags are evidence for an interest exception, never identity or
    # permission. Short Latin studio names need word boundaries (Key != monkey).
    texts = (document.title, *document.tags)
    for word in keywords:
        term = unicodedata.normalize("NFKC", word).casefold().strip()
        if not term:
            continue
        for text in texts:
            text = unicodedata.normalize("NFKC", text).casefold()
            if (re.search(r"(?<![a-z0-9])"+re.escape(term)+r"(?![a-z0-9])", text)
                    if term.isascii() and term.isalpha() and len(term) <= 8 else term in text):
                return True
    return False


def popularity_allows(document, min_views, min_comments, *, exempt_authors=(), exempt_keywords=()):
    """Verified authors are exempt; otherwise unknown counts fail strict thresholds."""
    if document is None:
        return False
    if document.ref.kind != "video":
        return True
    if document.author_id and document.author_id in {a for a in exempt_authors if a}:
        return True
    if exempt_keywords and interest_exempt(document, exempt_keywords):
        return True
    for value, minimum in ((document.view_count, min_views), (document.comment_count, min_comments)):
        if minimum is not None and (type(value) is not int or value <= minimum):
            return False
    return True


def reactive_video_reply(action, event):
    """Only the stored inbound notification can authorize this exact reply."""
    return bool(event and action.kind == "reply" and action.visibility == event.visibility == "public"
                and event.kind in {"mention", "reply"} and event.reply_to
                and action.reply_to == event.reply_to and action.content == event.content
                and action.platform == event.platform and action.account_id == event.account_id)
