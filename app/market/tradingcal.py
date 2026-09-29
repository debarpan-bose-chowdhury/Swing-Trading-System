"""NSE trading calendar: weekends and holidays from nse_calendar.json, plus special sessions."""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path


class Calendar:
    def __init__(self, path: str | Path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.holidays = set(data.get("holidays", []))
        self.special = set(data.get("specialSessions", []))

    def is_trading_day(self, day: date | str) -> bool:
        day = date.fromisoformat(str(day))
        if day.isoformat() in self.special:
            return True
        return day.weekday() < 5 and day.isoformat() not in self.holidays

    def days(self, start: date, end: date) -> list[date]:
        return [start + timedelta(n) for n in range((end - start).days + 1) if self.is_trading_day(start + timedelta(n))]

    def last_final_session(self, now: datetime, final_after: str) -> date:
        """Most recent trading day whose session is final (today only after final_after)."""
        day = now.date()
        if not (self.is_trading_day(day) and now.time() >= time.fromisoformat(final_after)):
            day -= timedelta(days=1)
            while not self.is_trading_day(day):
                day -= timedelta(days=1)
        return day
