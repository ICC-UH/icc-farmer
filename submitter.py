import logging
import random
import sqlite3
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import requests
from platforms.platform import BasePlatform, FlagSubmissionResult, get_platform
from shared import (
    BASE_URL,
    CAN_BATCH_SUBMIT_FLAG,
    DATABASE_PATH,
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
session: requests.Session
platform: 'BasePlatform'

stop_event: threading.Event = threading.Event()


def update_flag_status(results: list[FlagSubmissionResult]):
    try:
        with sqlite3.connect(DATABASE_PATH, timeout=8) as conn:
            updated: list[str] = []
            for result in results:
                _ = conn.execute(
                    'UPDATE flags SET status=? WHERE flag=? AND status=?',
                    (result.status, result.flag, FlagStatus.UNKNOWN),
                )
                updated.append(f'{result.flag}-{result.status}')

            conn.commit()

            if updated:
                for entry in range(0, len(updated), 4):
                    logger.info('\t' + ', '.join(updated[entry : entry + 4]))
    except Exception as e:
        logger.error(f'\tError updating flag status in database: {e}')


@dataclass
class SubmitOutcome:
    errors: list[Exception]
    results: list[FlagSubmissionResult]
    message: str | None = None


# FIXME: Bad retry concept, because what if the error is different each
#   time when retrying?
def submit_flags(
    flags: str | list[str], retries: int = 3, backoff: float = 2.0
) -> SubmitOutcome:
    exceptions: list[Exception] = []
    for attempt in range(1, retries + 1):
        try:
            if isinstance(flags, str):
                res = platform.submit_flag(flags)
            else:
                res = platform.submit_flags(flags)

            if isinstance(res, str):
                return SubmitOutcome([], [], res)

            return SubmitOutcome([], res if isinstance(res, list) else [res])
        except requests.RequestException as e:
            status = getattr(e.response, 'status_code', None)
            exceptions.append(e)

            # Only retry on connection/timeouts or 5xx errors
            if (
                attempt < retries
                and not stop_event.is_set()
                and (
                    isinstance(e, (requests.Timeout, requests.ConnectionError))
                    or (status and 500 <= status < 600)
                )
            ):
                _ = stop_event.wait(backoff)
                backoff = min(
                    backoff * 2 + random.uniform(0, 1), 30
                )  # Exponential backoff with jitter
                continue

            # For 4xx errors, don't bother retrying - it's our fault
            return SubmitOutcome(exceptions, [])
        except Exception as e:
            exceptions.append(e)
            return SubmitOutcome(exceptions, [])

    # This will never happen
    return SubmitOutcome(exceptions, [])


def submit_flags_batch(ex: ThreadPoolExecutor, flags: list[Flag]):
    futures: dict[Future[SubmitOutcome], list[Flag]] = {}
    for i in range(0, len(flags), SUBMITTER_BATCH_SIZE):
        if stop_event.is_set():
            break

        batch = flags[i : i + SUBMITTER_BATCH_SIZE]
        future = ex.submit(submit_flags, [flag.flag for flag in batch])
        futures[future] = batch

        _ = stop_event.wait(random.uniform(0.1, 0.25))

    for future in as_completed(futures):
        batch = futures[future]  # May be useful for future? (i mean future not future)
        result: SubmitOutcome = future.result()

        logger.info(
            f'Flag submit for Thread {threading.get_ident()} (batch size {len(batch)}):'
        )

        if result.errors:
            logger.error(f'\tFailed to submit flags: {result.errors}')
            continue

        if result.message is not None:
            logger.error(f'\tSubmission error: {result.message}')
            continue

        update_flag_status(result.results)


def submit_flags_individual(ex: ThreadPoolExecutor, flags: list[Flag]):
    futures: dict[Future[SubmitOutcome], Flag] = {}
    for flag in flags:
        if stop_event.is_set():
            break

        future = ex.submit(submit_flags, flag.flag)
        futures[future] = flag

        _ = stop_event.wait(random.uniform(0.1, 0.25))

    for future in as_completed(futures):
        _ = futures[future]  # May be useful for future?
        result = future.result()

        logger.info(f'Flag submit for Thread {threading.get_ident()}:')

        if result.errors:
            logger.error(f'\tFailed to submit flag: {result.errors}')
            continue

        if result.message is not None:
            logger.error(f'\tSubmission error: {result.message}')
            continue

        update_flag_status(result.results)


def main():
    try:
        _ = platform.login()
        logger.info(f'Logged in, token: {platform.token}')
    except requests.HTTPError as e:
        logger.critical(f'Failed to log in: {e}')
        sys.exit(1)

    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()

    try:
        logger.info('Starting flag submission loop...')
        while True:
            # logger.info('Checking for flags to submit...')

            _ = cursor.execute(
                'SELECT * FROM flags WHERE status = ?', (FlagStatus.UNKNOWN,)
            )
            flags = [Flag(*row[1:]) for row in cursor.fetchall()]  # pyright: ignore[reportAny]

            if flags:
                logger.info(f'Found {len(flags)} flags to submit.')
                with ThreadPoolExecutor(max_workers=SUBMITTER_MAX_WORKERS) as ex:
                    try:
                        if CAN_BATCH_SUBMIT_FLAG:
                            submit_flags_batch(ex, flags)
                        else:
                            submit_flags_individual(ex, flags)
                    except KeyboardInterrupt:
                        logger.info(
                            'Submission interrupted by user, cancelling pending submissions...'
                        )
                        stop_event.set()
                        try:
                            ex.shutdown(wait=True, cancel_futures=True)
                        except TypeError:
                            # For Python versions < 3.9 which do not support cancel_futures
                            ex.shutdown(wait=True)

                logger.info('Waiting for next submission...')
            # else:
            #     logger.info('No flags to submit.')

            if stop_event.is_set():
                break

            # logger.info(
            #     f'Sleeping for {SUBMITTER_WAKE} seconds before next submission...'
            # )
            time.sleep(SUBMITTER_WAKE)
    except KeyboardInterrupt:
        raise
    finally:
        cursor.close()
        conn.close()


if __name__ == '__main__':
    logger = setup_logging('1_submitter')

    session = requests.Session()
    platform = get_platform(PLATFORM, session, BASE_URL, USERNAME, PASSWORD, TOKEN)

    setup_database()

    try:
        main()
    except KeyboardInterrupt:
        logger.info('Received keyboard interrupt, stopping...')
    finally:
        session.close()
        logger.info('Exited cleanly')
