"""Source boundary: new providers can implement this interface without changing storage."""
from typing import Protocol
import pandas as pd


class KlineSource(Protocol):
    def archive(self, symbol: str, date: pd.Timestamp, monthly: bool = False) -> pd.DataFrame | None: ...
    def rest(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame: ...
