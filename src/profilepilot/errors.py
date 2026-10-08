"""Exception hierarchy. Messages are written to be shown to an AI model or a CLI user."""

from __future__ import annotations


class ProfilePilotError(Exception):
    """Base class for all expected, user-facing errors."""


class NotFoundError(ProfilePilotError):
    """A profile, proxy, tab or other referenced object does not exist."""


class AmbiguousError(ProfilePilotError):
    """A reference (name or id prefix) matches more than one object."""


class ConflictError(ProfilePilotError):
    """The operation conflicts with current state (duplicate name, stale revision, ...)."""


class ProfileRunningError(ConflictError):
    """The operation needs the profile to be stopped."""


class ProfileNotRunningError(ProfilePilotError):
    """The operation needs a running profile."""


class RestartRequiredError(ProfilePilotError):
    """A change cannot be applied live; the profile must be restarted."""


class LaunchError(ProfilePilotError):
    """The browser could not be started or attached."""


class BrowserNotFoundError(LaunchError):
    """No usable Chromium-family browser executable was found."""


class PolicyError(ProfilePilotError):
    """A request was blocked by the safety policy (e.g. remote mode URL restrictions)."""
