"""Flagsmith client for RecallDeck.

One multivariate flag does the experiment: ``study_mode`` with variations
``recall_first`` and ``reread`` (50/50 in the Flagsmith dashboard).

Rules that protect the experiment:
- The flag is looked up with the visitor's stable id, so the same visitor
  always gets the same variation. A fresh id per request would invalidate
  the experiment, so this module never invents an id.
- If Flagsmith is unreachable (bad key, no network, cold start), the
  default flag handler serves ``reread`` and the app keeps working.
- Events are sent to Flagsmith with track_event AND recorded in SQLite,
  so a Flagsmith outage never loses data. Event failures are swallowed:
  the app must never break because analytics failed.
"""
import os

from flagsmith import Flagsmith
from flagsmith.models import DefaultFlag

FLAG_NAME = "study_mode"
VARIATIONS = ("recall_first", "reread")
FALLBACK_VARIATION = "reread"


def _default_flag_handler(flag_name: str) -> DefaultFlag:
    return DefaultFlag(enabled=True, value=FALLBACK_VARIATION)


class FlagsmithClient:
    def __init__(self, environment_key: str | None = None, enabled: bool = True):
        self._key = environment_key or os.environ.get("FLAGSMITH_ENVIRONMENT_KEY")
        self._client: Flagsmith | None = None
        if self._key and enabled:
            try:
                self._client = Flagsmith(
                    environment_key=self._key,
                    enable_local_evaluation=True,
                    enable_events=True,
                    default_flag_handler=_default_flag_handler,
                    request_timeout_seconds=5,
                )
            except Exception:
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def get_variation(self, visitor_id: str) -> str:
        """Variation for one stable visitor id. Falls back to 'reread'."""
        if not self._client:
            return FALLBACK_VARIATION
        try:
            flag = self._client.get_experiment_flag(FLAG_NAME, visitor_id)
            value = str(flag.value) if flag is not None and flag.value is not None else None
            return value if value in VARIATIONS else FALLBACK_VARIATION
        except Exception:
            return FALLBACK_VARIATION

    def track_event(self, event: str, visitor_id: str,
                    value: int | float | None = None,
                    metadata: dict | None = None) -> None:
        if not self._client:
            return
        try:
            self._client.track_event(
                event, identifier=visitor_id, value=value, metadata=metadata or {}
            )
        except Exception:
            pass

    def flush(self) -> None:
        if not self._client:
            return
        try:
            self._client.flush_events()
        except Exception:
            pass
