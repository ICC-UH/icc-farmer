import concurrent
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
from dataclasses import dataclass
from signal import signal

import requests

from platforms.platform import (
    BasePlatform,
    PlatformChallenge,
    PlatformService,
    PlatformTeam,
    PlatformUser,
    get_platform,
)
from shared import (
    BASE_URL,
    DATABASE_PATH,
    FARMER_MAX_WORKERS,
    FARMER_TIMEOUT,
    FARMER_WAKE,
    FLAG_PREFIX,
    PASSWORD,
    PLATFORM,
    SKIP_OUR_TEAM,
    SKIP_OUR_TEAM_IP,
    TOKEN,
    USERNAME,
    Flag,
    FlagStatus,
    setup_database,
    setup_logging,
)

filename: str

logger: logging.Logger
session: requests.Session = None
platform: 'BasePlatform' = None

child_procs = set()
child_procs_lock = threading.Lock()
stop_event = threading.Event()

FLAG_REGEX = re.compile(re.escape(FLAG_PREFIX) + r'[A-Za-z0-9_\-+=/\.]{32,128}\}')


def insert_flag(flag: Flag):
    try:
        with sqlite3.connect(DATABASE_PATH, timeout=10) as conn:
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


def register_child(proc: subprocess.Popen) -> None:
    with child_procs_lock:
        child_procs.add(proc)


def unregister_child(proc: subprocess.Popen) -> None:
    with child_procs_lock:
        child_procs.discard(proc)


# WHAT THE FUCK? So mANY neSTED TRY CAtCH
def terminate_child(proc: subprocess.Popen):
    # Try to kill the whole process group (so grandchildren die too).
    try:
        if proc.poll() is None:
            try:
                if os.name == 'nt':
                    # best-effort Windows method
                    try:
                        # Send CTRL_BREAK_EVENT to the process group, then ensure kill.
                        proc.send_signal(signal.CTRL_BREAK_EVENT)
                        time.sleep(0.2)
                    except Exception:
                        pass
                    if proc.poll() is None:
                        proc.kill()
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                # best effort
                try:
                    proc.kill()
                except Exception:
                    logger.debug(
                        'Could not kill child pid=%s', getattr(proc, 'pid', None)
                    )
    except Exception:
        logger.debug('Error while terminating child pid=%s', getattr(proc, 'pid', None))
    finally:
        unregister_child(proc)


def terminate_childrens():
    logger.info('Terminating all child processes...')
    with child_procs_lock:
        procs = list(child_procs)

    for proc in procs:
        terminate_child(proc)


# FIXME: Bad retry concept, because what if the error is different each
#   time when retrying?
# TODO: Refactor this to make it more readable. Or maybe not just refactor
#   this function, but the whole file.
def run_exploit(
    ip: str, port: int, filename: str, retries=3, backoff=2
) -> tuple[bytes, bytes, int, bool]:
    cwd = os.path.dirname(os.path.abspath(filename)) or None
    file = os.path.basename(filename)

    for attempt in range(1, retries + 1):
        if stop_event.is_set():
            return b'', b'Cancelled', -1, False

        proc = None
        out, err = b'', b''

        try:
            if os.name == 'nt':
                proc = subprocess.Popen(
                    [sys.executable, file, ip, str(port)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                proc = subprocess.Popen(
                    [sys.executable, file, ip, str(port)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    preexec_fn=os.setsid,
                )

            register_child(proc)

            try:
                out, err = proc.communicate(timeout=FARMER_TIMEOUT)
            except KeyboardInterrupt:
                print('Exploit interrupted by user, cancelling...')
            except subprocess.TimeoutExpired:
                terminate_child(proc)

                # attempt to collect any remaining output
                try:
                    out, err = proc.communicate(timeout=5)
                except Exception:
                    out, err = out or b'', err or b''

                return out, err, -1, True  # Indicate timeout

            rc = proc.returncode

            if attempt < retries and rc != 0:
                unregister_child(proc)

                time.sleep(backoff)
                backoff = min(
                    backoff * 2 + random.uniform(0, 1), 30
                )  # Exponential backoff with jitter
                continue

            return out or b'', err or b'', rc, False
        except Exception as e:
            return b'', str(e).encode(), -1, False
        finally:
            if proc:
                unregister_child(proc)


@dataclass
class ServiceDetails:
    ip: str
    port: int
    team_id: int
    team_name: str
    challenge_id: int
    challenge_name: str


def exploit_services(
    teams: list[PlatformTeam],
    challenges: list[PlatformChallenge],
    services: list[PlatformService],
    filename: str,
):
    with ThreadPoolExecutor(max_workers=FARMER_MAX_WORKERS) as ex:
        try:
            futures: dict[concurrent.futures.Future, ServiceDetails] = {}
            for service in services:
                try:
                    ip, port_str = service.addresses[0].rsplit(':', 1)
                    ip = ip
                    port = int(port_str)
                except (ValueError, AttributeError):
                    raise ValueError(
                        f'Invalid address format: {service.addresses[0]!r}'
                    )

                service_detail = ServiceDetails(
                    ip=ip,
                    port=port,
                    team_id=service.team_id or -1,
                    team_name=next(
                        (team.name for team in teams if team.id == service.team_id),
                        'Unknown Team',
                    ),
                    challenge_id=service.challenge_id or -1,
                    # challenge_name=next(
                    #     (
                    #         challenge.title
                    #         for challenge in challenges
                    #         if challenge.id == service.challenge_id
                    #     ) if challenges else [],
                    #     'Unknown Challenge',
                    # ),
                    challenge_name='Unknown Challenge',
                )

                if SKIP_OUR_TEAM:
                    if platform in ['ailurus']:
                        me: PlatformUser = platform.get_me()
                        if service_detail.team_id == me.team_id:
                            continue
                    else:
                        if SKIP_OUR_TEAM_IP in service_detail.ip:
                            continue

                logger.info(
                    f'Running exploit against {service_detail.team_name} ({service_detail.team_id}) ({ip}:{port})'
                )

                fut = ex.submit(run_exploit, ip, port, filename)
                futures[fut] = service_detail

            for future in as_completed(futures):
                service_detail = futures[future]

                logger.info(
                    f'Exploit result from {service_detail.team_name} ({service_detail.team_id}) ({service_detail.ip}:{service_detail.port}):'
                )

                out, err, code, timeout = future.result()
                if timeout:
                    logger.error(f'\tExploit timed out after {FARMER_TIMEOUT} seconds.')
                    continue

                if code != 0:
                    if out:
                        logger.debug('\tstdout:')
                        for line in out.decode().splitlines():
                            logger.debug(f'\t\t{line}')

                    if err:
                        logger.error('\tstderr:')
                        for line in err.decode().splitlines():
                            logger.error(f'\t\t{line}')

                    logger.info(f'\tReturn code: {code}')
                    continue

                flags = FLAG_REGEX.findall(out.decode())
                if not flags:
                    logger.warning('\tNo flag found.')
                    continue

                flags = list(set(flags))

                for flag in flags:
                    logger.info(f'\tFound flag: {flag}')
                    insert_flag(
                        Flag(
                            team_id=service_detail.team_id,
                            team_name=service_detail.team_name,
                            challenge_id=service_detail.challenge_id,
                            challenge_name=service_detail.challenge_name,
                            flag=flag,
                            status=FlagStatus.UNKNOWN,
                        ),
                    )
        except KeyboardInterrupt:
            logger.info(
                'Exploitation interrupted by user, cancelling pending exploits...'
            )
            stop_event.set()
            terminate_childrens()
            ex.shutdown(wait=False, cancel_futures=True)
            raise


def main():
    try:
        _ = platform.login()
        logger.info(f'Logged in, token: {platform.token}')
    except requests.HTTPError as e:
        logger.critical(f'Failed to log in: {e}')
        sys.exit(1)

    # i hate this..
    challenge_id = -1
    port = -1

    teams = None
    challenges = None

    teams = list(platform.list_teams())
    if teams:
        logger.info(f'Found {len(teams)} teams.')
        for team in teams:
            logger.info(f'\tTeam {team.id}: {team.name}')

    if PLATFORM in ['ailurus']:
        challenges = list(platform.list_challenges())
        if challenges:
            logger.info(f'Found {len(challenges)} challenges.')
            for challenge in challenges:
                logger.info(f'\tChallenge {challenge.id}: {challenge.title}')

        challenge_id = int(input('Enter challenge ID to target: ').strip())
    else:
        port = int(input('Enter service port to target: ').strip())

    while True:
        # also hate this stupid code
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
        except requests.RequestException as e:
            logger.error(f'Network error fetching services: {e}')
            stop_event.wait(FARMER_WAKE)
            continue
        except ValueError as e:
            logger.error(f'Error fetching services: {e}')
            stop_event.wait(FARMER_WAKE)
            continue

        if not services:
            logger.warning('No services found, retrying after sleep...')
            stop_event.wait(FARMER_WAKE)
            continue

        # what the fuck is this?
        if challenge_id == -1:
            for service in services:
                try:
                    ip_str = service.addresses[0].rsplit(':', 1)[0]
                except (ValueError, AttributeError, IndexError):
                    # If the address is malformed or missing, skip modifying this service
                    continue
                # Assign to the attribute rather than using item assignment on the object
                try:
                    print(ip_str)
                    setattr(service, 'addresses', [f'{ip_str}:{port}'])
                except Exception:
                    # Best-effort: if we can't set the attribute, skip modifying this service
                    continue

        exploit_services(teams, challenges, services, filename)

        if stop_event.is_set():
            break

        logger.info(f'Sleeping for {FARMER_WAKE} seconds before next round...')
        time.sleep(FARMER_WAKE)


if __name__ == '__main__':
    # hate this stupid global, but whatever
    if len(sys.argv) < 2:
        print(f'Usage: python3 {sys.argv[0]} exploit.py')
        sys.exit(1)

    filename = sys.argv[1]
    if not filename.endswith('.py') or not os.path.exists(filename):
        print(f'Invalid or missing exploit file: {filename}')
        sys.exit(1)

    log_file_name = os.path.basename(filename).replace('.py', '')
    logger = setup_logging('2_farmer', log_file_name)

    session = requests.Session()
    platform = get_platform(PLATFORM, session, BASE_URL, USERNAME, PASSWORD, TOKEN)

    setup_database()

    try:
        main()
    except KeyboardInterrupt:
        logger.info('Received keyboard interrupt, stopping...')
    finally:
        logger.info('Exited cleanly')
