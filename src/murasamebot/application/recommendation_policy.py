def popularity_allows(document, min_views, min_comments):
    """Unknown counts never satisfy an enabled threshold; comparisons are strict."""
    if document is None:
        return False
    if document.ref.kind != "video":
        return True
    for value, minimum in ((document.view_count, min_views), (document.comment_count, min_comments)):
        if minimum is not None and (type(value) is not int or value <= minimum):
            return False
    return True
