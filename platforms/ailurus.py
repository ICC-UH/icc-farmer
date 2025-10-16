from __future__ import annotations

import typing as t

from platforms.platform import (
    BasePlatform,
    FlagSubmissionResult,
    PlatformChallenge,
    PlatformService,
    PlatformTeam,
)


class Platform(BasePlatform):
    def login(self) -> t.Union[str, dict]:
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

        return data

    def is_logged_in(self) -> bool:
        return bool(self.token)

    def list_teams(self) -> t.Iterator[PlatformTeam]:
        res = self.session.get(f'{self.base_url}/api/v2/teams', timeout=5)
        res.raise_for_status()

        for team in res.json().get('data', []):
            yield PlatformTeam(id=int(team.get('id')), name=team.get('name'))

    def list_challenges(self) -> t.Iterator[PlatformChallenge]:
        res = self.session.get(f'{self.base_url}/api/v2/challenges', timeout=5)
        res.raise_for_status()

        for challenge in res.json().get('data', []):
            yield PlatformChallenge(
                id=int(challenge.get('id')), title=challenge.get('title')
            )

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

    def submit_flag(self, flag: str) -> t.Union[str, FlagSubmissionResult]:
        if not isinstance(flag, str):
            raise ValueError('flag must be a string')

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

    def submit_flags(
        self, flags: t.List[str]
    ) -> t.Union[str, t.List[FlagSubmissionResult]]:
        if not isinstance(flags, list):
            raise ValueError('flags must be a list of strings')

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

    def _process_flag_result(self, verdict: dict, flag: str) -> FlagSubmissionResult:
        status_map = {
            'flag is correct.': 'accepted',
            'flag is wrong or expired.': 'rejected',
            'flag already submitted.': 'already_submitted',
        }
        return FlagSubmissionResult(
            flag=flag, status=status_map.get(verdict, 'unknown')
        )
