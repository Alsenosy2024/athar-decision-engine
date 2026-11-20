"""Wall-clock budgets shared by nested interactive optimization calls."""
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

_search_deadline = ContextVar('optimization_search_deadline', default=None)


class SearchExpired(RuntimeError):
    """The parent search stopped this unfinished candidate."""


def check_search_time():
    deadline = _search_deadline.get()
    if deadline is not None and monotonic() >= deadline:
        raise SearchExpired


class SolveBudget:
    def __init__(self, seconds):
        self.limit = seconds
        self.deadline = monotonic() + seconds
        parent = _search_deadline.get()
        if parent is not None:
            self.deadline = min(self.deadline, parent)

    def remaining(self):
        return max(0, self.deadline - monotonic())

    @contextmanager
    def share(self):
        token = _search_deadline.set(self.deadline)
        try:
            yield
        finally:
            _search_deadline.reset(token)
