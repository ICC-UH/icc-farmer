import logging
import random
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from platforms.platform import get_platform
from shared import (
    BASE_URL,
    CAN_BATCH_SUBMIT,
    MAX_SUBMITTER_WORKERS,
    PASSWORD,
    PLATFORM,
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


def update_flag_status(logger, results):
    try:
        with sqlite3.connect('flags.db', timeout=10) as conn:
            for result in results:
                conn.execute(
                    'UPDATE flags SET status=? WHERE flag=?', (result.get('status'), result.get('flag'))
                )
                logger.info(f"\tFlag {result.get('flag')} status updated to {result.get('status')}")
            conn.commit()
    except Exception as e:
        logger.error(f'\tError updating flag status in database: {e}')


# FIXME: Bad retry concept, because what if the error is different each
# time when retrying?
def submit_flags(flags: str | list[str], retries=3, backoff=2) -> dict:
    if not CAN_BATCH_SUBMIT and not isinstance(flags, str):
        raise ValueError(
            'When CAN_BATCH_SUBMIT is False, flags must be a single flag string.'
        )

    exceptions = []
    for attempt in range(1, retries + 1):
        try:
            if isinstance(flags, str):
                return '', platform.submit_flag(flags)

            return '', platform.submit_flags(flags)
        except requests.RequestException as e:
            exceptions.append(e)
            if attempt < retries:
                time.sleep(backoff)
                backoff = min(backoff * 2 + random.uniform(0, 1), 30)  # Exponential backoff with jitter
                continue

            return exceptions, {}
        except Exception as e:
            return e, {}


def submit_flags_batch(logger, flags):
    with ThreadPoolExecutor(max_workers=MAX_SUBMITTER_WORKERS) as executor:
        batch_size = 16
        futures = {
            executor.submit(
                submit_flags, [flag.flag for flag in flags[i : i + batch_size]]
            ): flags[i : i + batch_size]
            for i in range(0, len(flags), batch_size)
        }

        for future in as_completed(futures):
            batch = futures[future]
            message, results = future.result()

            logger.info(f'Flag submit for {", ".join(flag.flag for flag in batch)}:')
            if message:
                logger.error(f'\tFailed to submit flags: {message}')
            elif isinstance(results, str):
                logger.error(f'\tSubmission error: {results}')
            else:
                update_flag_status(logger, results)


def submit_flags_individual(logger, flags):
    with ThreadPoolExecutor(max_workers=MAX_SUBMITTER_WORKERS) as executor:
        futures = {executor.submit(submit_flags, flag.flag): flag for flag in flags}

        for future in as_completed(futures):
            flag = futures[future]
            message, result = future.result()

            logger.info(f'Flag submit for {flag.flag}:')
            if message:
                logger.error(f'\tFailed to submit flag: {message}')
            elif isinstance(result, str):
                logger.error(f'\tSubmission error: {result}')
            else:
                update_flag_status(logger, [result])


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

    conn = sqlite3.connect('flags.db')
    cursor = conn.cursor()

    while True:
        logger.info('Checking for flags to submit...')

        cursor.execute('SELECT * FROM flags WHERE status = ?', (FlagStatus.UNKNOWN,))
        flags = [Flag(*row[1:]) for row in cursor.fetchall()]

        if not flags:
            logger.info('No flags to submit, retrying after sleep...')
            time.sleep(SUBMITTER_WAKE)
            continue

        if CAN_BATCH_SUBMIT:
            submit_flags_batch(logger, flags)
        else:
            submit_flags_individual(logger, flags)

        logger.info(f'Sleeping for {SUBMITTER_WAKE} seconds before next submission...')
        time.sleep(SUBMITTER_WAKE)


if __name__ == '__main__':
    logger = setup_logging('1_submitter')
    setup_database()

    try:
        main(logger)
    except KeyboardInterrupt:
        logger.info('Received keyboard interrupt, stopping...')
    finally:
        logger.info('Exited cleanly')
