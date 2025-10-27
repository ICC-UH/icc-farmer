from __future__ import annotations

import base64
import json
import re
import typing as t

from platforms.platform import (
    BasePlatform,
    FlagSubmissionResult,
    PlatformChallenge,
    PlatformService,
    PlatformTeam,
    PlatformUser,
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
    def get_me(self) -> PlatformUser:
        if not self.is_logged_in():
            raise ValueError('Not logged in')

        token = self.session.headers.get('Authorization').split(' ')[1]
        payload = self._parse_jwt(token)
        if payload is None:
            raise ValueError('Invalid token')

        sub = payload.get('sub')
        if sub is None:
            raise ValueError('Invalid token payload')

        return PlatformUser(
            team_id=-1,
            team_name=sub,
        )

    @override
    def list_teams(self) -> t.Iterator[PlatformTeam]:
        res = self.session.get(f'{self.base_url}/api/user', timeout=5)
        res.raise_for_status()

        for team in res.json():
            yield PlatformTeam(id=int(team.get('id')), name=team.get('username'))

    @override
    def list_challenges(self) -> t.Iterator[PlatformChallenge]:
        res = self.session.get(f'{self.base_url}/api/challenges', timeout=5)
        res.raise_for_status()

        for challenge in res.json():
            yield PlatformChallenge(
                id=int(challenge.get('id')), title=challenge.get('title'), port=challenge.get('port')
            )

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
        match = re.match(r'([A-Za-z0-9]{2,})\{([A-Za-z0-9-_]{32,})\}', flag)
        if not match:
            raise ValueError('flag must be in the format PREFIX{BASE64}')

        flag_content = match.group(2)
        if not flag_content:
            raise ValueError('base64 flag is empty')

        res = self.session.post(
            f'{self.base_url}/api/flag', json={'flag': flag_content}, timeout=5
        )
        data = res.json()

        # Only raise for server errors
        if 500 <= res.status_code < 600:
            res.raise_for_status()

        return self._process_flag_result(data.get('message', 'unknown'), f'{flag}')

    def _parse_jwt(self, token: str) -> t.Optional[dict]:
        try:
            base64_url = token.split('.')[1]
            base64_url += '=' * (-len(base64_url) % 4)  # Pad base64 string
            base64_bytes = base64_url.replace('-', '+').replace('_', '/')
            json_payload = base64.b64decode(base64_bytes).decode('utf-8')
            return json.loads(json_payload)
        except Exception:
            return None

    def _process_flag_result(self, verdict: dict, flag: str) -> FlagSubmissionResult:
        status_map = {
            'Flag submitted successfully': 'accepted',
            'Invalid flag': 'rejected',
            'Flag has already been submitted': 'already_submitted',
            'Cannot submit your own flag': 'own_flag',
        }
        return FlagSubmissionResult(
            flag=flag, status=status_map.get(verdict, 'unknown')
        )
