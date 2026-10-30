"""Read-only video provider for explicitly private delivery, no public filters."""


class PrivateVideoProvider:
    def __init__(self, adapter):
        self.adapter = adapter

    def search(self, query, category_ids, limit):
        return self.adapter.search(query, category_ids, limit, topic_filter=False)

    def read(self, ref, **kwargs):
        return self.adapter.read(ref, topic_filter=False, **kwargs)
