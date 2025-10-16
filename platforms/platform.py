from __future__ import annotations

import importlib
import typing as t

import requests


class BasePlatform:
    """Minimal platform interface for CTF platforms."""

    def __init__(
        self,
        session: requests.Session,
        base_url: str,
        username: str = '',
        password: str = '',
        token: str = '',
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip('/')
        self.username = username
        self.password = password
        self.token = token

    def login(self) -> t.Union[str, dict]:
        """Authenticate and return token/data."""
        raise NotImplementedError()

    def is_logged_in(self) -> bool:
        """Check if the current session is authenticated."""
        raise NotImplementedError()

    def list_teams(self) -> t.Iterator[dict]:
        """Yield available teams."""
        raise NotImplementedError()

    def list_challenges(self) -> t.Iterator[dict]:
        """Yield available challenges."""
        raise NotImplementedError()

    def get_services(self, filter_: dict) -> t.Iterator[dict]:
        """Yield services filtered by the given criteria."""
        raise NotImplementedError()

    def submit_flag(self, flag: str) -> t.Union[str, dict]:
        """Submit a single flag and return the result."""
        raise NotImplementedError()

    def submit_flags(self, flags: t.List[str]) -> t.Union[str, t.List[dict]]:
        """Submit multiple flags and return the results."""
        raise NotImplementedError()


def get_platform(
    name: str,
    session: requests.Session,
    base_url: str,
    username: str = '',
    password: str = '',
    token: str = '',
) -> BasePlatform:
    """Dynamically import and instantiate a platform implementation."""
    module_name = f'platforms.{name}'
    try:
        mod = importlib.import_module(module_name)
    except ImportError as e:
        raise ImportError(f"Failed to import platform module '{module_name}': {e}")

    if not hasattr(mod, 'Platform'):
        raise ImportError(f"Module '{module_name}' does not define a 'Platform' class")

    cls = getattr(mod, 'Platform')
    if not issubclass(cls, BasePlatform):
        raise TypeError(f"'Platform' in '{module_name}' must subclass 'BasePlatform'")

    return cls(
        session=session,
        base_url=base_url,
        username=username,
        password=password,
        token=token,
    )
