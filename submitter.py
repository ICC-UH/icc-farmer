import logging
import random
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from platforms.platform import get_platform
from shared import (
    BASE_URL,
    CAN_BATCH_SUBMIT,
    SUBMITTER_MAX_WORKERS,
    PASSWORD,
    PLATFORM,
    SUBMITTER_BATCH_SIZE,
    SUBMITTER_WAKE,
    TOKEN,
    USERNAME,
    Flag,
    FlagStatus,
    setup_database,
    setup_logging,
)

session = requests.Session()
platform = get_platform(PLATFORM, session, BASE_URL, USERNAME, PASSWORD, TOKEN)


def update_flag_status(logger: logging.Logger, results: list[dict]):
    try:
        with sqlite3.connect('flags.db', timeout=10) as conn:
            updated = []
            for result in results:
                conn.execute(
                    'UPDATE flags SET status=? WHERE flag=?',
                    (result.get('status'), result.get('flag')),
                )
                updated.append(f'{result.get("flag")}-{result.get("status")}')

            conn.commit()

            if updated:
                for entry in range(0, len(updated), 4):
                    logger.info('\t' + ', '.join(updated[entry : entry + 4]))
    except Exception as e:
        logger.error(f'\tError updating flag status in database: {e}')


# FIXME: Bad retry concept, because what if the error is different each
# time when retrying?
def submit_flags(
    flags: str | list[str], retries=3, backoff=2
) -> tuple[Exception | list[Exception], dict] | tuple[Exception, dict]:
    if not CAN_BATCH_SUBMIT and not isinstance(flags, str):
        raise ValueError(
            'When CAN_BATCH_SUBMIT is False, flags must be a single flag string.'
        )

    exceptions = []
    for attempt in range(1, retries + 1):
        try:
            if isinstance(flags, str):
                return None, platform.submit_flag(flags)

            return None, platform.submit_flags(flags)
        except requests.RequestException as e:
            status = getattr(e.response, 'status_code', None)
            exceptions.append(e)

            # Only retry on connection/timeouts or 5xx errors
            if attempt < retries and (
                isinstance(e, (requests.Timeout, requests.ConnectionError))
                or (status and 500 <= status < 600)
            ):
                time.sleep(backoff)
                backoff = min(
                    backoff * 2 + random.uniform(0, 1), 30
                )  # Exponential backoff with jitter
                continue

            # For 4xx errors, don't bother retrying - it's our fault
            return exceptions, {}
        except Exception as e:
            return e, {}


# Should we add delay between submissions to avoid rate limiting? since
# the batch size is too low?
def submit_flags_batch(logger: logging.Logger, flags: list[Flag]):
    with ThreadPoolExecutor(max_workers=SUBMITTER_MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                submit_flags,
                [flag.flag for flag in flags[i : i + SUBMITTER_BATCH_SIZE]],
            ): flags[i : i + SUBMITTER_BATCH_SIZE]
            for i in range(0, len(flags), SUBMITTER_BATCH_SIZE)
        }

        for future in as_completed(futures):
            batch = futures[future]  # May be useful for future?
            message, results = future.result()

            logger.info(
                f'Flag submit for Thread {threading.get_ident()} (batch size {len(batch)}):'
            )
            if message:
                logger.error(f'\tFailed to submit flags: {message}')
            elif isinstance(results, str):
                logger.error(f'\tSubmission error: {results}')
            else:
                update_flag_status(logger, results)


# TODO: Add delay between submissions to avoid rate limiting
def submit_flags_individual(logger: logging.Logger, flags: list[Flag]):
    with ThreadPoolExecutor(max_workers=SUBMITTER_MAX_WORKERS) as executor:
        futures = {executor.submit(submit_flags, flag.flag): flag for flag in flags}

        for future in as_completed(futures):
            # flag = futures[future] # May be useful for future?
            message, result = future.result()

            logger.info(f'Flag submit for Thread {threading.get_ident()}:')
            if message:
                logger.error(f'\tFailed to submit flag: {message}')
            elif isinstance(result, str):
                logger.error(f'\tSubmission error: {result}')
            else:
                update_flag_status(logger, [result])


def main(logger: logging.Logger, stop_set: threading.Event):
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

    conn = sqlite3.connect('flags.db')
    cursor = conn.cursor()

    while not stop_set.is_set():
        logger.info('Checking for flags to submit...')

        cursor.execute('SELECT * FROM flags WHERE status = ?', (FlagStatus.UNKNOWN,))
        flags = [Flag(*row[1:]) for row in cursor.fetchall()]

        if flags:
            if CAN_BATCH_SUBMIT:
                submit_flags_batch(logger, flags)
            else:
                submit_flags_individual(logger, flags)
        else:
            logger.info('No flags to submit.')

        logger.info(f'Sleeping for {SUBMITTER_WAKE} seconds before next submission...')
        stop_set.wait(SUBMITTER_WAKE)

    cursor.close()
    conn.close()


if __name__ == '__main__':
    logger = setup_logging('1_submitter')
    setup_database()

    stop_set = threading.Event()

    try:
        main(logger, stop_set)
    except KeyboardInterrupt:
        stop_set.set()
        logger.info('Received keyboard interrupt, stopping...')
    finally:
        logger.info('Exited cleanly')
