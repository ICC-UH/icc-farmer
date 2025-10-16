import logging
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from platforms.platform import get_platform
from shared import (
    BASE_URL,
    FARMER_TIMEOUT,
    FARMER_WAKE,
    FLAG_PREFIX,
    MAX_FARMER_WORKERS,
    PASSWORD,
    PLATFORM,
    TOKEN,
    USERNAME,
    Flag,
    FlagStatus,
    setup_database,
    setup_logging,
)

session = requests.Session()
platform = get_platform(PLATFORM, session, BASE_URL, USERNAME, PASSWORD, TOKEN)


def insert_flag_into_db(logger, flag: Flag):
    """Insert a flag into the database."""
    try:
        with sqlite3.connect('flags.db', timeout=10) as conn:
            conn.execute(
                """
                INSERT INTO flags (team_id, team_name, challenge_id, challenge_name, flag, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    flag.team_id,
                    flag.team_name,
                    flag.challenge_id,
                    flag.challenge_name,
                    flag.flag,
                    flag.status,
                ),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        logger.warning(f'\t\tFlag {flag.flag} already exists in the database.')
    except Exception as e:
        logger.error(f'\t\tError inserting flag into database: {e}')


# FIXME: Bad retry concept, because what if the error is different each
# time when retrying?
# TODO: Stop the exploit when CTRL+C is pressed
def run_exploit(ip: str, port: int, filename: str, retries=3, backoff=2) -> str:
    for attempt in range(1, retries + 1):
        try:
            cwd = os.path.dirname(os.path.abspath(filename)) or None
            file = os.path.basename(filename)

            proc = subprocess.run(
                ['python3', file, ip, str(port)],
                capture_output=True,
                timeout=FARMER_TIMEOUT,
                cwd=cwd,
            )
            proc.check_returncode()
            return proc.stdout, proc.stderr, proc.returncode, False
        except subprocess.CalledProcessError as e:
            if attempt < retries:
                time.sleep(backoff)
                backoff = min(
                    backoff * 2 + random.uniform(0, 1), 30
                )  # Exponential backoff with jitter
                continue

            return e.stdout or b'', e.stderr or b'', e.returncode, False
        except subprocess.TimeoutExpired as e:
            return e.stdout or b'', e.stderr or b'', -1, True
        except Exception as e:
            return b'', str(e).encode(), -1, False


# TODO: Refactor this shit
def main(logger: logging.Logger):
    try:
        data = platform.login()
        if not data:
            logger.error('Login failed, no data returned.')
            sys.exit(1)

        if isinstance(data, str):
            logger.info(f'Logged in, token: {data}')
        elif isinstance(data, dict):
            logger.info(f'Logged in, data: {data}')
    except requests.RequestException as e:
        logger.error(f'Failed to login to platform: {e}')
        sys.exit(1)

    teams = list(platform.list_teams())

    if teams:
        for team in teams:
            logger.info(f'Found team: {team["id"]} - {team["name"]}')

    challenge_id = -1
    challenges = list(platform.list_challenges())

    if challenges:
        for challenge in challenges:
            logger.info(f'Found challenge: {challenge["id"]} - {challenge["title"]}')

        challenge_id = int(input('Enter challenge ID to target: ').strip())
    else:
        port = int(input('Enter service port to target: ').strip())

    while True:
        try:
            services = list(
                platform.get_services(
                    {
                        'challenge_id': challenge_id,
                    }
                    if challenge_id != -1
                    else {}
                )
            )
        except Exception as e:
            logger.error(f'Error fetching services: {e}')
            time.sleep(FARMER_WAKE)
            continue

        if not services:
            logger.warning('No services found, retrying after sleep...')
            time.sleep(FARMER_WAKE)
            continue

        # Randomize the order to avoid hitting the same target repeatedly in a row
        indices = list(range(len(services)))
        random.shuffle(indices)

        with ThreadPoolExecutor(max_workers=MAX_FARMER_WORKERS) as ex:
            futures = {}

            for i in indices:
                ip = services[i]['addresses'][0].split(':')[0]
                port = (
                    services[i]['addresses'][0].split(':')[1]
                    if challenge_id != -1
                    else port
                )

                logger.info(f'Running exploit against {ip}:{port}...')

                fut = ex.submit(run_exploit, ip, port, filename)
                futures[fut] = services[i]

            for future in as_completed(futures):
                details = futures[future]

                ip = details['addresses'][0].split(':')[0]
                port = (
                    details['addresses'][0].split(':')[1]
                    if challenge_id != -1
                    else port
                )

                team_id = details.get('team_id', 0)
                team_name = details.get(
                    'team_name',
                    next((t['name'] for t in teams if int(t['id']) == int(team_id)), 'unknown'),
                )

                challenge_id = details.get('challenge_id', 0)
                challenge_name = details.get(
                    'challenge_name',
                    next(
                        (c['title'] for c in challenges if int(c['id']) == int(challenge_id)),
                        'unknown',
                    ),
                )

                logger.info(
                    f'Exploit result from {team_name} ({team_id}) ({ip}:{port}):'
                )

                out, err, code, timeout = future.result()
                if timeout:
                    logger.error(f'\tExploit timed out after {FARMER_TIMEOUT} seconds.')
                    continue

                if not out:
                    logger.error(
                        f'\tNo output from exploit, return code: {code}, stderr:'
                    )
                    if err:
                        for line in err.decode().splitlines():
                            logger.error(f'\t\t{line}')

                    continue

                if code != 0:
                    logger.error(f'\tNon-zero return code from exploit: {code}.')
                    if out:
                        logger.error('\t\tstdout:')
                        for line in out.decode().splitlines():
                            logger.error(f'\t\t{line}')

                    if err:
                        logger.error('\t\tstderr:')
                        for line in err.decode().splitlines():
                            logger.error(f'\t\t{line}')

                    continue

                flags = re.findall(
                    re.escape(FLAG_PREFIX) + r'.*?\}', out.decode().strip()
                )
                if not flags:
                    logger.warning('\tNo flag found.')
                    continue

                flags = list(set(flags))

                for flag in flags:
                    logger.info(f'\tFound flag: {flag}')
                    insert_flag_into_db(
                        logger,
                        Flag(
                            team_id=team_id,
                            team_name=team_name,
                            challenge_id=challenge_id,
                            challenge_name=challenge_name,
                            flag=flag,
                            status=FlagStatus.UNKNOWN,
                        ),
                    )

        logger.info(f'Sleeping for {FARMER_WAKE} seconds before next round...')
        time.sleep(FARMER_WAKE)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f'Usage: python3 {sys.argv[0]} exploit.py')
        sys.exit(1)

    filename = sys.argv[1]
    if not filename.endswith('.py') or not os.path.exists(filename):
        print(f'Invalid or missing exploit file: {filename}')
        sys.exit(1)

    log_file_name = os.path.basename(filename).replace('.py', '')
    logger = setup_logging('2_farmer', log_file_name)

    try:
        setup_database()
        main(logger)
    except KeyboardInterrupt:
        logger.info('Received keyboard interrupt, stopping...')
    finally:
        logger.info('Exited cleanly')
