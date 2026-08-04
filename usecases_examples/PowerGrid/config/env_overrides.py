"""
Environment-variable overrides for the PowerGrid simulator configuration.

Before this module the simulator read no environment variables at all: every
value came from ``config/CONFIG.toml`` and ``config/API_POWERGRID_CAB.toml``,
both baked into the image, and both written *back to* by the running app. That
made per-deployment configuration impossible without rebuilding the image, and
made the container non-disposable because it accumulated state in its own
filesystem.

This module introduces a single, documented precedence order:

    explicit API parameters  >  environment variables  >  TOML file

The TOML files remain the default source, so an operator who starts the image
with no environment set sees exactly the previous behaviour. That property is
deliberate — it keeps this fork mergeable against upstream and keeps the
standalone ``docker compose up`` in ``usecases_examples/PowerGrid`` working.

Spec coverage: FR-30, FR-31, FR-32
"""

import logging
import os
from typing import Any, Callable, Dict, List, NamedTuple, Optional

# Environment variable holding the CAB platform base URL. ``HAI_CAB_URL`` is the
# name used by the WP3 compose files; ``CAB_API_URL`` is the name the WP3 service
# passes into the container today. Both are accepted so neither side has to
# change first, and the WP3 name wins if somebody sets both.
CAB_URL_ENVIRONMENT_VARIABLES = ("HAI_CAB_URL", "CAB_API_URL")


class _EnvironmentOverride(NamedTuple):
    """One environment variable mapped onto one simulator configuration key.

    Attributes:
        variable_name: Environment variable read from the process environment.
        config_key: Key in ``CONFIG.toml`` this variable replaces.
        parse: Callable converting the raw string to the type the simulator
            expects. Parsing failures are logged and the override is skipped,
            never raised — a malformed variable must not stop a session from
            starting on the file defaults.
    """

    variable_name: str
    config_key: str
    parse: Callable[[str], Any]


# Every key in CONFIG.toml, with the environment variable that overrides it.
#
# Two spellings are offered for the values a WP3 session actually varies:
# HAI_SCENARIO and HAI_AGENT are the names the WP3 service already passes at
# container launch (where they were previously ignored), and the HAI_SIM_*
# family covers the remaining simulation tuning parameters. The config keys are
# taken verbatim from CONFIG.toml, including the inconsistent casing of
# ``stepDuration_s``, because Simulator reads them by exact name.
CONFIG_OVERRIDES: List[_EnvironmentOverride] = [
    _EnvironmentOverride("HAI_SCENARIO", "scenario_name", str),
    _EnvironmentOverride("HAI_AGENT", "assistant_path", str),
    _EnvironmentOverride("HAI_SIM_ENV_NAME", "env_name", str),
    _EnvironmentOverride("HAI_SIM_ENV_SEED", "env_seed", int),
    _EnvironmentOverride("HAI_SIM_ASSISTANT_SEED", "assistant_seed", int),
    _EnvironmentOverride("HAI_SIM_TIME_STEP_FORECAST", "time_step_forecast", int),
    _EnvironmentOverride("HAI_SIM_DURATION_STEP_FORECAST", "duration_step_forecast", int),
    _EnvironmentOverride("HAI_SIM_REFRESH_FREQUENCY_STEP", "refresh_frequency_step", int),
    _EnvironmentOverride("HAI_SIM_STEP_START_SECURITY_ANALYSIS", "step_start_security_analysis", int),
    _EnvironmentOverride("HAI_SIM_STEP_DURATION_S", "stepDuration_s", float),
    _EnvironmentOverride("HAI_SIM_SCENARIO_FIRST_STEP", "scenario_first_step", int),
]


def collect_config_overrides() -> Dict[str, Any]:
    """
    Read every recognised simulator variable from the environment.

    Variables that are unset or empty are skipped, so an empty string behaves
    the same as an absent variable. This matters because Docker Compose renders
    an unset ``${VAR}`` interpolation as an empty string rather than omitting
    the variable, and an empty scenario name would otherwise reach grid2op.

    Returns:
        Configuration keys mapped to parsed values, containing only the keys
        that were actually present in the environment. Empty when the process
        environment carries no simulator configuration.

    Example:
        >>> os.environ["HAI_SCENARIO"] = "jan_28_1"
        >>> collect_config_overrides()
        {'scenario_name': 'jan_28_1'}
    """
    overrides: Dict[str, Any] = {}

    for override in CONFIG_OVERRIDES:
        raw_value = os.environ.get(override.variable_name, "").strip()
        if not raw_value:
            continue

        try:
            overrides[override.config_key] = override.parse(raw_value)
        except (TypeError, ValueError):
            # A malformed override falls back to the TOML default rather than
            # failing the session. The log line is the operator's only signal,
            # so it names both the variable and the offending value.
            logging.warning(
                "Ignoring malformed environment override %s=%r; "
                "falling back to the value in CONFIG.toml",
                override.variable_name,
                raw_value,
            )

    return overrides


def resolve_cab_url() -> Optional[str]:
    """
    Resolve the CAB platform base URL from the environment.

    ``API_POWERGRID_CAB.toml`` ships a list of hardcoded addresses on the
    ``192.168.x.x`` range plus a public demo host, none of which resolve on a
    deployment's own network. When this returns a URL, it is prepended to the
    server list so it becomes the default selection while the file entries stay
    available for manual selection in the UI.

    Returns:
        The configured CAB base URL, or None when the environment sets none, in
        which case the file's addresses are used unchanged.
    """
    for variable_name in CAB_URL_ENVIRONMENT_VARIABLES:
        cab_url = os.environ.get(variable_name, "").strip()
        if cab_url:
            return cab_url

    return None


def configuration_is_externally_managed() -> bool:
    """
    Report whether configuration comes from outside the image.

    When true, the simulator must not persist configuration back into its TOML
    files: the environment or the WP3 control API is the source of truth, and
    writing to the image filesystem would both defeat the override on the next
    read and leave per-session state inside a container that is reused across
    sessions (FR-32).

    Returns:
        True when any simulator configuration variable or CAB URL variable is
        set in the environment.
    """
    return bool(collect_config_overrides()) or resolve_cab_url() is not None


def describe_active_overrides() -> str:
    """
    Render the active overrides as a single log-friendly line.

    Used once at startup so an operator debugging a session can see what the
    simulator actually resolved, rather than inferring it from the absence of
    an error.

    Returns:
        A human-readable summary, or a statement that the TOML defaults apply.
    """
    overrides = collect_config_overrides()
    cab_url = resolve_cab_url()

    if not overrides and cab_url is None:
        return "No environment overrides; using CONFIG.toml and API_POWERGRID_CAB.toml as shipped."

    described_parts = ["{}={!r}".format(key, value) for key, value in sorted(overrides.items())]
    if cab_url is not None:
        described_parts.append("cab_url={!r}".format(cab_url))

    return "Environment overrides active: " + ", ".join(described_parts)
