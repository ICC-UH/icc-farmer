import enum
import logging
import os
import sqlite3
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

PLATFORM = 'wreckit'
BASE_URL = 'https://wreckit-api.siberlab.id/'

USERNAME = ''
PASSWORD = ''
TOKEN = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJmcmVzaCI6ZmFsc2UsImlhdCI6MTc2MTEwMjk0MywianRpIjoiMTIxNDExMmUtYTM2OC00ZGNmLWEzZmUtZTUyNjIxOWQ4YjQ0IiwidHlwZSI6ImFjY2VzcyIsInN1YiI6IklDQyBaMk0iLCJuYmYiOjE3NjExMDI5NDMsImNzcmYiOiI3Nzc2ZDZlZi01OWZhLTQ3NjQtODAxNC1mNmRlMTAyYjkxODciLCJleHAiOjE3NjExODkzNDN9.7-otID9i2XjyPY1MDSF8z4vMqBYo-ff7kUP_fBoBwH0'  # for ailurus, and WreckIt

FLAG_PREFIX = 'WRECKIT6{'
CAN_BATCH_SUBMIT_FLAG = False
SKIP_OUR_TEAM = True
SKIP_OUR_TEAM_IP = '18.141.207.253'  # for WreckIt

INTERVAL = 60 * 5
TOTAL_TEAM = 10

FARMER_WAKE = max(8, (INTERVAL // 2) - 8)
FARMER_TIMEOUT = max(4, (FARMER_WAKE // 2) - 4)
FARMER_MAX_WORKERS = 2

SUBMITTER_WAKE = 1#max(4, (INTERVAL // TOTAL_TEAM) - 4)
SUBMITTER_MAX_WORKERS = 4
SUBMITTER_BATCH_SIZE = min(100, TOTAL_TEAM * 2)  # ailurus maximum batch submit is 100

LOGS_PATH = './logs'
DATABASE_PATH = './flags.db'


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


class NormalFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
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

    def format(self, record: logging.LogRecord) -> str:
        original_levelname = record.levelname
        levelname = f'{record.levelname:<8}'  # Pad to width 8
        color = self.COLORS.get(record.levelname, '')
        record.levelname = f'{color}{levelname}{self.RESET}'
        result = super().format(record)
        record.levelname = original_levelname  # restore for other handlers
        return result


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


def setup_database():
    with sqlite3.connect(DATABASE_PATH, timeout=8) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER,
                team_name TEXT,
                challenge_id INTEGER,
                challenge_name TEXT,
                flag TEXT,
                status TEXT
            )
        """)
        c.commit()
