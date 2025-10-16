import logging
import random
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from platforms.platform import BasePlatform, get_platform
from shared import (
    BASE_URL,
    CAN_BATCH_SUBMIT,
    PASSWORD,
    PLATFORM,
    SUBMITTER_BATCH_SIZE,
    SUBMITTER_MAX_WORKERS,
    SUBMITTER_WAKE,
    TOKEN,
    USERNAME,
    Flag,
    FlagStatus,
    setup_database,
    setup_logging,
)

logger: logging.Logger

session: requests.Session = None
platform: 'BasePlatform' = None

stop_event: threading.Event = threading.Event()


def update_flag_status(results: list[dict]):
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
#   time when retrying?
def submit_flags(
    flags: str | list[str], retries=3, backoff=2
) -> tuple[Exception | list[Exception], dict] | tuple[Exception, dict]:
    exceptions = []
    for attempt in range(1, retries + 1):
        if stop_event.is_set():
            return Exception('Submission cancelled'), {}

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
        except BaseException as e:
            return e, {}


# Should we add delay between submissions to avoid rate limiting? since
# the batch size is too low?
def submit_flags_batch(flags: list[Flag]):
    with ThreadPoolExecutor(max_workers=SUBMITTER_MAX_WORKERS) as ex:
        # I hate this code, but I can't think of a cleaner way to do it
        futures = {
            ex.submit(
                submit_flags,
                [flag.flag for flag in flags[i : i + SUBMITTER_BATCH_SIZE]],
            ): flags[i : i + SUBMITTER_BATCH_SIZE]
            for i in range(0, len(flags), SUBMITTER_BATCH_SIZE)
        }

        try:
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
                    update_flag_status(results)
        except KeyboardInterrupt:
            logger.info(
                'Submission interrupted by user, cancelling pending submissions...'
            )
            stop_event.set()
            ex.shutdown(wait=False, cancel_futures=True)


# TODO: Add delay between submissions to avoid rate limiting
def submit_flags_individual(flags: list[Flag]):
    with ThreadPoolExecutor(max_workers=SUBMITTER_MAX_WORKERS) as ex:
        futures = {ex.submit(submit_flags, flag.flag): flag for flag in flags}

        try:
            for future in as_completed(futures):
                # flag = futures[future] # May be useful for future?
                message, result = future.result()

                logger.info(f'Flag submit for Thread {threading.get_ident()}:')
                if message:
                    logger.error(f'\tFailed to submit flag: {message}')
                elif isinstance(result, str):
                    logger.error(f'\tSubmission error: {result}')
                else:
                    update_flag_status([result])
        except KeyboardInterrupt:
            logger.info(
                'Submission interrupted by user, cancelling pending submissions...'
            )
            stop_event.set()
            ex.shutdown(wait=False, cancel_futures=True)


def main():
    try:
        platform.login()
        logger.info(f'Logged in, token: {platform.token}')
    except requests.HTTPError as e:
        logger.critical(f'Failed to log in: {e}')
        sys.exit(1)

    conn = sqlite3.connect('flags.db')
    cursor = conn.cursor()

    while not stop_event.is_set():
        logger.info('Checking for flags to submit...')

        cursor.execute('SELECT * FROM flags WHERE status = ?', (FlagStatus.UNKNOWN,))
        flags = [Flag(*row[1:]) for row in cursor.fetchall()]

        try:
            if flags:
                if CAN_BATCH_SUBMIT:
                    submit_flags_batch(flags)
                else:
                    submit_flags_individual(flags)
            else:
                logger.info('No flags to submit.')
        except KeyboardInterrupt:
            raise

        logger.info(f'Sleeping for {SUBMITTER_WAKE} seconds before next submission...')
        stop_event.wait(SUBMITTER_WAKE)

    cursor.close()
    conn.close()


if __name__ == '__main__':
    # hate this stupid global, but whatever
    logger = setup_logging('1_submitter')
    setup_database()

    session = requests.Session()
    platform = get_platform(PLATFORM, session, BASE_URL, USERNAME, PASSWORD, TOKEN)

    try:
        main()
    except KeyboardInterrupt:
        stop_event.set()
        logger.info('Received keyboard interrupt, stopping...')
    finally:
        logger.info('Exited cleanly')
