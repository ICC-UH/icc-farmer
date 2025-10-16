import logging
import os
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from signal import signal

import requests

from platforms.platform import get_platform
from shared import (
    BASE_URL,
    FARMER_MAX_WORKERS,
    FARMER_TIMEOUT,
    FARMER_WAKE,
    FLAG_PREFIX,
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

child_procs = set()
child_procs_lock = threading.Lock()


def insert_flag_into_db(logger, flag: Flag):
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

        logger.info(f'\tInserted flag {flag.flag} into the database.')
    except sqlite3.IntegrityError:
        logger.debug(f'\tFlag {flag.flag} already exists in the database.')
    except Exception as e:
        logger.error(f'\tError inserting flag into database: {e}')


# FIXME: Bad retry concept, because what if the error is different each
# time when retrying?
def run_exploit(
    ip: str, port: int, filename: str, retries=3, backoff=2
) -> tuple[bytes, bytes, int, bool]:
    cwd = os.path.dirname(os.path.abspath(filename)) or None
    file = os.path.basename(filename)

    for attempt in range(1, retries + 1):
        proc = None

        try:
            proc = subprocess.Popen(
                [sys.executable, file, ip, str(port)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                preexec_fn=os.setsid if os.name != 'nt' else None,
            )

            with child_procs_lock:
                child_procs.add(proc)

            try:
                proc_stdout, proc_stderr = proc.communicate(timeout=FARMER_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    if os.name == 'nt':
                        proc.send_signal(signal.CTRL_BREAK_EVENT)
                        time.sleep(0.2)
                    else:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    proc.kill()

                with child_procs_lock:
                    if proc in child_procs:
                        child_procs.remove(proc)

                proc_stdout, proc_stderr = proc.communicate(timeout=5)
                return proc_stdout, proc_stderr, -1, True  # Indicate timeout

            if attempt < retries and proc.returncode != 0:
                with child_procs_lock:
                    if proc in child_procs:
                        child_procs.remove(proc)

                time.sleep(backoff)
                backoff = min(
                    backoff * 2 + random.uniform(0, 1), 30
                )  # Exponential backoff with jitter
                continue

            return proc_stdout, proc_stderr, proc.returncode, False
        except Exception as e:
            return b'', str(e).encode(), -1, False
        finally:
            with child_procs_lock:
                if proc in child_procs:
                    child_procs.remove(proc)


def parse_address(address: str) -> tuple[str, int]:
    if ':' not in address:
        raise ValueError(f'Invalid address format: {address}')

    ip, port_str = address.rsplit(':', 1)
    try:
        port = int(port_str)
    except ValueError:
        raise ValueError(f'Invalid port in address: {address}')

    return ip, port


def parse_details(
    details: dict, teams: list[dict], challenges: list[dict]
) -> tuple[int, str, int, str]:
    team_id = details.get('team_id', 0)
    team_name = details.get(
        'team_name',
        next(
            (t['name'] for t in teams if int(t['id']) == int(team_id)),
            'unknown',
        ),
    )

    challenge_id = details.get('challenge_id', 0)
    challenge_name = details.get(
        'challenge_name',
        next(
            (c['title'] for c in challenges if int(c['id']) == int(challenge_id)),
            'unknown',
        ),
    )

    return team_id, team_name, challenge_id, challenge_name


def print_process_output(logger, out: bytes, err: bytes, code: int):
    if out:
        logger.debug('\tstdout:')
        for line in out.decode().splitlines():
            logger.debug(f'\t\t\t{line}')

    if err:
        logger.error('\tstderr:')
        for line in err.decode().splitlines():
            logger.error(f'\t\t{line}')

    logger.info(f'\tReturn code: {code}')


def exploit_services(teams, challenges, services: list[dict], filename: str):
    with ThreadPoolExecutor(max_workers=FARMER_MAX_WORKERS) as ex:
        futures = {}

        for service in services:
            ip, port = parse_address(service['addresses'][0])
            team_id, team_name, challenge_id, challenge_name = parse_details(
                service, teams, challenges
            )

            # Don't attack ourselves?
            # if team_id == 3:
            #     continue

            logger.info(
                f'Running exploit against {team_name} ({team_id}) ({ip}:{port})'
            )

            fut = ex.submit(run_exploit, ip, port, filename)
            futures[fut] = service

        for future in as_completed(futures):
            service = futures[future]

            ip, port = parse_address(service['addresses'][0])
            team_id, team_name, challenge_id, challenge_name = parse_details(
                service, teams, challenges
            )

            logger.info(f'Exploit result from {team_name} ({team_id}) ({ip}:{port}):')

            out, err, code, timeout = future.result()
            if timeout:
                logger.error(f'\tExploit timed out after {FARMER_TIMEOUT} seconds.')
                continue

            if code != 0:
                print_process_output(logger, out, err, code)
                continue

            flags = re.findall(re.escape(FLAG_PREFIX) + r'.*?\}', out.decode().strip())
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


def main(logger: logging.Logger, stop_set: threading.Event, filename: str):
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

    challenge_id = -1
    port = -1

    teams = list(platform.list_teams())
    if teams:
        logger.info(f'Found {len(teams)} teams.')
        for team in teams:
            logger.info(f'\tTeam {team["id"]}: {team["name"]}')

    challenges = list(platform.list_challenges())
    if challenges:
        logger.info(f'Found {len(challenges)} challenges.')
        for challenge in challenges:
            logger.info(f'\tChallenge {challenge["id"]}: {challenge["title"]}')

        challenge_id = int(input('Enter challenge ID to target: ').strip())
    else:
        port = int(input('Enter service port to target: ').strip())

    while not stop_set.is_set():
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
            stop_set.wait(FARMER_WAKE)
            continue

        if not services:
            logger.warning('No services found, retrying after sleep...')
            stop_set.wait(FARMER_WAKE)
            continue

        if challenge_id == -1:
            for service in services:
                service['addresses'] = [
                    f'{service["addresses"][0].split(":")[0]}:{port}'
                ]

        exploit_services(teams, challenges, services, filename)

        logger.info(f'Sleeping for {FARMER_WAKE} seconds before next round...')
        stop_set.wait(FARMER_WAKE)


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

    stop_set = threading.Event()

    try:
        setup_database()
        main(logger, stop_set, filename)
    except KeyboardInterrupt:
        with child_procs_lock:
            for proc in child_procs:
                try:
                    if os.name == 'nt':
                        proc.send_signal(signal.CTRL_BREAK_EVENT)
                        time.sleep(0.2)
                    else:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    proc.kill()

            child_procs.clear()

        stop_set.set()
        logger.info('Received keyboard interrupt, stopping...')
    finally:
        logger.info('Exited cleanly')
