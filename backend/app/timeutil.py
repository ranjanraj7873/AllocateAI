from datetime import datetime, timezone


def utcnow() -> datetime:
    """Naive UTC datetime. Naive (not tz-aware) is deliberate: SQLite round-
    trips SQLAlchemy DateTime columns as naive datetimes, so every stored
    timestamp and every comparison against one needs to agree on that or
    comparisons raise TypeError. Using datetime.now(timezone.utc) and
    dropping the tzinfo (rather than the bare, now-deprecated
    datetime.utcnow()) gets the same value without the deprecation warning.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
