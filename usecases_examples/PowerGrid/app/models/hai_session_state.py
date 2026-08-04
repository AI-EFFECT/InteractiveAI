"""
Lifecycle state of the WP3 session this simulator process is serving.

The simulator's engine is a module-level singleton: ``com``, ``simu`` and the
simulation loop all belong to the process, not to a request. That makes one
container capable of serving exactly one session at a time, which is why WP3
runs a *pool* of simulator containers and hands each session a free one rather
than sharing a single container between participants.

This module holds the small amount of state needed to make that arrangement
safe: which session a container is currently bound to, and how far that session
has progressed. WP3's control plane reads it to decide whether a container is
free, and to detect a session that ended so the slot can be released.

State transitions:

    idle ──configure──> configured ──stream opens──> running
     ^                                                  │
     └──────────────── reset ◀── finished ◀── stream ends┘

Spec coverage: FR-34
"""

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# No session is bound; the container is free to be assigned.
SESSION_STATE_IDLE = "idle"
# A session is bound and the grid2op environment is loaded, but the participant
# has not opened the simulation stream yet.
SESSION_STATE_CONFIGURED = "configured"
# The participant's browser is consuming the simulation stream.
SESSION_STATE_RUNNING = "running"
# The episode ended or the stream closed. The container still holds the
# environment, so it must be reset before it can be reassigned.
SESSION_STATE_FINISHED = "finished"


class HumanAISessionState:
    """
    Thread-safe record of the session currently bound to this simulator.

    Every mutation is guarded because the simulation stream runs in a different
    thread (or greenlet, under eventlet) from the control-plane requests that
    configure and query it. Without the lock, a control request could observe a
    half-written transition — for instance a session id already cleared while
    the state still reads ``running``.
    """

    def __init__(self) -> None:
        """Create an unbound state record in the idle state."""
        self._lock = threading.Lock()
        self._state = SESSION_STATE_IDLE
        self._session_id: Optional[str] = None
        self._scenario_name: Optional[str] = None
        self._last_transition_at = datetime.now(timezone.utc)

    def mark_configured(self, session_id: str, scenario_name: Optional[str] = None) -> None:
        """
        Bind this simulator to a WP3 session whose environment is now loaded.

        Args:
            session_id: WP3 session identifier, used to correlate the
                simulator with the trace and survey results it produces.
            scenario_name: Name of the grid2op chronic that was loaded, as
                reported by the environment. Recorded for diagnostics.
        """
        with self._lock:
            self._state = SESSION_STATE_CONFIGURED
            self._session_id = session_id
            self._scenario_name = scenario_name
            self._last_transition_at = datetime.now(timezone.utc)

    def mark_running(self) -> None:
        """Record that the participant has opened the simulation stream."""
        with self._lock:
            self._state = SESSION_STATE_RUNNING
            self._last_transition_at = datetime.now(timezone.utc)

    def mark_finished(self) -> None:
        """
        Record that the simulation stream ended.

        Called from the stream's ``finally`` block, so it runs whether the
        episode completed normally, raised, or the participant simply closed
        the browser tab.
        """
        with self._lock:
            self._state = SESSION_STATE_FINISHED
            self._last_transition_at = datetime.now(timezone.utc)

    def reset(self) -> None:
        """Unbind the session and return this simulator to the free pool."""
        with self._lock:
            self._state = SESSION_STATE_IDLE
            self._session_id = None
            self._scenario_name = None
            self._last_transition_at = datetime.now(timezone.utc)

    def is_occupied(self) -> bool:
        """
        Report whether a session is currently bound to this simulator.

        A finished session still counts as occupied: its environment is loaded
        and its state is readable, so the slot is not reusable until WP3 has
        collected the outcome and issued a reset.

        Returns:
            True unless the simulator is idle.
        """
        with self._lock:
            return self._state != SESSION_STATE_IDLE

    def snapshot(self) -> Dict[str, Any]:
        """
        Return a consistent copy of the current state.

        Returns:
            Mapping with ``state``, ``session_id``, ``scenario_name`` and
            ``last_transition_at`` (ISO 8601, UTC). Suitable for returning
            directly as the body of ``GET /hai/state``.
        """
        with self._lock:
            return {
                "state": self._state,
                "session_id": self._session_id,
                "scenario_name": self._scenario_name,
                "last_transition_at": self._last_transition_at.isoformat(),
            }


# Process-wide instance. One simulator process serves one session, so a single
# shared record is the correct scope — mirroring how ``com`` and ``simu`` are
# themselves module-level singletons in the app.
state = HumanAISessionState()
