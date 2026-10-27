from threading import Condition


class ModelConcurrency:
    """One adjustable request limit shared by text and vision clients in a worker."""

    def __init__(self, limit: int):
        self.condition = Condition()
        self.limit, self.active = limit, 0

    def resize(self, limit: int):
        with self.condition:
            self.limit = limit
            self.condition.notify_all()

    def __enter__(self):
        with self.condition:
            self.condition.wait_for(lambda: self.active < self.limit)
            self.active += 1
        return self

    def __exit__(self, *args):
        with self.condition:
            self.active -= 1
            self.condition.notify_all()
