from __future__ import annotations

import base64
import json
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

        res = self.session.post(
            f'{self.base_url}/api/v2/authenticate',
            json={'email': self.username, 'password': self.password},
            timeout=5,
        )
        res.raise_for_status()

        data = res.json().get('data', '')
        self.token = data
        if self.token:
            self.session.headers.update({'Authorization': f'Bearer {self.token}'})

        return self.token

    @override
    def is_logged_in(self) -> bool:
        if self.session.headers.get('Authorization') is None:
            return False

        # There is api/v2/token-check/
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
        team = sub.get('team')
        if sub is None or team is None:
            raise ValueError('Invalid token payload')

        return PlatformUser(
            team_id=int(team.get('id')),
            team_name=team.get('name'),
        )

    @override
    def list_teams(self) -> t.Iterator[PlatformTeam]:
        res = self.session.get(f'{self.base_url}/api/v2/teams', timeout=5)
        res.raise_for_status()

        for team in res.json().get('data', []):
            yield PlatformTeam(id=int(team.get('id')), name=team.get('name'))

    @override
    def list_challenges(self) -> t.Iterator[PlatformChallenge]:
        res = self.session.get(f'{self.base_url}/api/v2/challenges', timeout=5)
        res.raise_for_status()

        for challenge in res.json().get('data', []):
            yield PlatformChallenge(
                id=int(challenge.get('id')), title=challenge.get('title')
            )

    @override
    def get_services(self, filter_: dict) -> t.Iterator[PlatformService]:
        if 'challenge_id' not in filter_:
            raise ValueError("filter_ must contain 'challenge_id'")

        challenge_id = filter_['challenge_id']
        res = self.session.get(
            f'{self.base_url}/api/v2/challenges/{challenge_id}/services', timeout=5
        )
        res.raise_for_status()

        for team_id, addresses in res.json().get('data', {}).items():
            yield PlatformService(
                addresses=addresses,
                challenge_id=challenge_id,
                team_id=int(team_id),
            )

    @override
    def submit_flag(self, flag: str) -> t.Union[str, FlagSubmissionResult]:
        res = self.session.post(
            f'{self.base_url}/api/v2/submit', json={'flag': flag}, timeout=5
        )
        data = res.json()

        if not data.get('data', {}):
            return data.get('message', 'Unknown error')

        # Only raise for server errors
        if 500 <= res.status_code < 600:
            res.raise_for_status()

        return self._process_flag_result(data.get('message', 'unknown'), flag)

    @override
    def submit_flags(
        self, flags: t.List[str]
    ) -> t.Union[str, t.List[FlagSubmissionResult]]:
        res = self.session.post(
            f'{self.base_url}/api/v2/submit', json={'flags': flags}, timeout=5
        )
        data = res.json()

        if data.get('status') == 'failed':
            return data.get('message', 'Unknown error')

        # Only raise for server errors
        if 500 <= res.status_code < 600:
            res.raise_for_status()

        return [
            self._process_flag_result(
                flag_data.get('verdict', 'unknown'), flag_data.get('flag')
            )
            for flag_data in data.get('data', [])
        ]

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
            'flag is correct.': 'accepted',
            'flag is wrong or expired.': 'rejected',
            'flag already submitted.': 'already_submitted',
        }
        return FlagSubmissionResult(
            flag=flag, status=status_map.get(verdict, 'unknown')
        )
