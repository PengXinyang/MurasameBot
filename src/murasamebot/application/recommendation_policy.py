def popularity_allows(document, min_views, min_comments, *, exempt_authors=()):
    """Verified authors are exempt; otherwise unknown counts fail strict thresholds."""
    if document is None:
        return False
    if document.ref.kind != "video":
        return True
    if document.author_id and document.author_id in {a for a in exempt_authors if a}:
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
