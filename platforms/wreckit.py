from __future__ import annotations

import re
import typing as t

from platforms.platform import (
    BasePlatform,
    FlagSubmissionResult,
    PlatformService,
    PlatformTeam,
)
from typing_extensions import override


class Platform(BasePlatform):
    @override
    def login(self) -> str:
        if self.token:
            self.session.headers.update({'Authorization': f'Bearer {self.token}'})
            return self.token

        raise ValueError('Token is required for login')

    @override
    def is_logged_in(self) -> bool:
        if self.session.headers.get('Authorization') is None:
            return False

        return True

    @override
    def list_teams(self) -> t.Iterator[PlatformTeam]:
        res = self.session.get(f'{self.base_url}/api/user', timeout=5)
        res.raise_for_status()

        for team in res.json():
            yield PlatformTeam(id=int(team.get('id')), name=team.get('username'))

    @override
    def get_services(self, filter_: dict) -> t.Iterator[PlatformService]:
        res = self.session.get(f'{self.base_url}/api/user', timeout=5)
        res.raise_for_status()

        for service in res.json():
            yield PlatformService(
                addresses=[service.get('host_ip')],
                team_id=int(service.get('id')),
            )

    @override
    def submit_flag(self, flag: str) -> t.Union[str, FlagSubmissionResult]:
        match = re.match(r'[A-Za-z0-9]{2,}\{([A-Za-z0-9-_]{32,})\}', flag)
        if not match:
            raise ValueError('flag must be in the format PREFIX{BASE64}')

        flag = match.group(1)
        if not flag:
            raise ValueError('base64 flag is empty')

        res = self.session.post(f'{self.base_url}/flag', json={'flag': flag}, timeout=5)
        data = res.json()

        # Only raise for server errors
        if 500 <= res.status_code < 600:
            res.raise_for_status()

        return self._process_flag_result(data.get('message', 'unknown'), flag)

    def _process_flag_result(self, verdict: dict, flag: str) -> FlagSubmissionResult:
        status_map = {
            'Flag submitted successfully': 'accepted',
        }
        return FlagSubmissionResult(
            flag=flag, status=status_map.get(verdict, 'unknown')
        )
