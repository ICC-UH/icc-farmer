import enum
import logging
import os
import sqlite3
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

FLAG_PREFIX = 'ICC{'
CAN_BATCH_SUBMIT = True

BASE_URL = 'http://192.168.18.2:5000/'

INTERVAL = 60 * 2
TOTAL_TEAM = 3

FARMER_WAKE = max(1, (INTERVAL // 2))
FARMER_TIMEOUT = max(1, (FARMER_WAKE // 2))
FARMER_MAX_WORKERS = 2

SUBMITTER_WAKE = max(4, (INTERVAL // TOTAL_TEAM) - 4)
SUBMITTER_MAX_WORKERS = 4
SUBMITTER_BATCH_SIZE = min(100, TOTAL_TEAM * 2)

LOGS_PATH = './logs'

PLATFORM = 'ailurus'
USERNAME = 'u2@test.com'
PASSWORD = 'GmLkj@d34WGe!vW'
TOKEN = ''


class NormalFormatter(logging.Formatter):
    def format(self, record):
        levelname = f'{record.levelname:<8}'  # Pad to width 8
        record.levelname = f'{levelname}'
        return super().format(record)


class ColoredFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[36m',  # Cyan
        'INFO': '\033[32m',  # Green
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',  # Red
        'CRITICAL': '\033[41m',  # Red background
    }
    RESET = '\033[0m'

    def format(self, record):
        original_levelname = record.levelname
        levelname = f'{record.levelname:<8}'  # Pad to width 8
        color = self.COLORS.get(record.levelname, '')
        record.levelname = f'{color}{levelname}{self.RESET}'
        result = super().format(record)
        record.levelname = original_levelname  # restore for other handlers
        return result


class FlagStatus(str, enum.Enum):
    UNKNOWN = 'unknown'
    SUBMITTING = 'submitting'  # just stay unknown when submitting?
    ACCEPTED = 'accepted'
    REJECTED = 'rejected'
    ALREADY_SUBMITTED = 'already_submitted'


@dataclass
class Flag:
    team_id: int
    team_name: str
    challenge_id: int
    challenge_name: str
    flag: str
    status: str = FlagStatus.UNKNOWN


def setup_database():
    with sqlite3.connect('flags.db', timeout=8) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER,
                team_name TEXT,
                challenge_id INTEGER,
                challenge_name TEXT,
                flag TEXT UNIQUE,
                status TEXT
            )
        """)
        c.commit()


def setup_logging(name: str, filename: str = '') -> logging.Logger:
    if not os.path.exists(LOGS_PATH):
        os.makedirs(LOGS_PATH)

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    ch = logging.StreamHandler()
    ch.setFormatter(ColoredFormatter('%(levelname)s - %(message)s'))
    logger.addHandler(ch)

    fh = RotatingFileHandler(
        f'{LOGS_PATH}/{name}{"_" + filename if filename else ""}.log',
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    fh.setFormatter(NormalFormatter('%(levelname)s - %(message)s'))
    logger.addHandler(fh)

    return logger
