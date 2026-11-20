"""Recurring weekly service calendars for an explicit DES scenario."""
from itertools import pairwise

from pydantic import Field, model_validator

from app.enterprise.schema import StrictFields

WEEK_MINUTES = 7 * 24 * 60
WEEK = WEEK_MINUTES * 60


class Shift(StrictFields):
    start: int = Field(ge=0, lt=WEEK_MINUTES, strict=True)
    end: int = Field(gt=0, le=WEEK_MINUTES, strict=True)

    @model_validator(mode='after')
    def ordered(self):
        if self.start >= self.end:
            raise ValueError('Shift end must be after its start')
        return self


class ShiftSchedule(StrictFields):
    windows: list[Shift] = Field(min_length=1, max_length=42)

    @model_validator(mode='after')
    def no_overlap(self):
        self.windows.sort(key=lambda shift: shift.start)
        if any(left.end > right.start for left, right in pairwise(self.windows)):
            raise ValueError('Weekly shift windows must not overlap')
        return self

    @property
    def seconds_open(self):
        return sum((shift.end - shift.start) * 60 for shift in self.windows)

    def next_window(self, now):
        week = int(now // WEEK) * WEEK
        for shift in self.windows:
            start, end = week + shift.start * 60, week + shift.end * 60
            if end > now:
                return max(now, start), end
        first = self.windows[0]
        return week + WEEK + first.start * 60, week + WEEK + first.end * 60
