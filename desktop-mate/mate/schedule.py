from dataclasses import dataclass
from datetime import datetime, time
from math import ceil


@dataclass(frozen=True)
class Schedule:
    start: time = time(9)
    lunch_start: time = time(12)
    lunch_end: time = time(13)
    end: time = time(18)

    @property
    def valid(self) -> bool:
        return self.start <= self.lunch_start < self.lunch_end <= self.end

    def display(self, now: datetime) -> tuple[str, str]:
        current = now.time()
        if current >= self.end:
            return "퇴근 시간이에요!", "오늘도 수고했어요"

        seconds = (datetime.combine(now.date(), self.end) - now).total_seconds()
        hours, remainder = divmod(ceil(seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        if current < self.start:
            status = "출근 전이에요"
        elif self.lunch_start <= current < self.lunch_end:
            status = "점심시간이에요"
        elif hours == 0:
            status = "퇴근이 가까워요!"
        else:
            status = "업무 중이에요"
        return f"퇴근까지 {hours:02d}:{minutes:02d}:{seconds:02d}", status
