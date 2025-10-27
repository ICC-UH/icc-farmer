import enum
import logging
import os
import sqlite3
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

from typing_extensions import override

PLATFORM = 'gemastik25'
BASE_URL = 'https://gemastik-api.siberlab.id/'

USERNAME = ''
PASSWORD = ''
TOKEN = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJmcmVzaCI6ZmFsc2UsImlhdCI6MTc2MTU1NDMzMCwianRpIjoiOWM3ZjZiYWEtNTQ1OS00MTk2LWFhZGUtZjUwMDA1OWJiY2FjIiwidHlwZSI6ImFjY2VzcyIsInN1YiI6IklDQyBQaXNhbmcgTW9sZW4iLCJuYmYiOjE3NjE1NTQzMzAsImNzcmYiOiIyMTk2YWU4NS0xMDY0LTQzZWItYjViOS0wZTRlZjE3NzMyYmQiLCJleHAiOjE3NjE2NDA3MzB9.Cm0fnBFVUFsOMKLwZlBrQdvubL414EX2fbIdMmvWaXw'  # for ailurus, and WreckIt

FLAG_PREFIX = 'GEMASTIK18{'
CAN_BATCH_SUBMIT_FLAG = False
SKIP_OUR_TEAM = True
SKIP_OUR_TEAM_IP = '13.229.198.100'  # for WreckIt

INTERVAL = 60 * 5
TOTAL_TEAM = 10

FARMER_WAKE = max(8, (INTERVAL // 2) - 8)
FARMER_TIMEOUT = 32  # max(4, (FARMER_WAKE // 2) - 4)
FARMER_MAX_WORKERS = 2

SUBMITTER_WAKE = 1  # max(4, (INTERVAL // TOTAL_TEAM) - 4)
SUBMITTER_MAX_WORKERS = 4
SUBMITTER_BATCH_SIZE = min(100, TOTAL_TEAM * 2)  # ailurus maximum batch submit is 100

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_PATH = os.path.join(BASE_DIR, 'logs')
DATABASE_PATH = os.path.join(BASE_DIR, 'flags.db')


class FlagStatus(str, enum.Enum):
    UNKNOWN = 'unknown'
    SUBMITTING = 'submitting'  # just stay unknown when submitting?
    ACCEPTED = 'accepted'
    REJECTED = 'rejected'
    ALREADY_SUBMITTED = 'already_submitted'
    OWN_FLAG = 'own_flag'


@dataclass
class Flag:
    team_id: int
    team_name: str
    challenge_id: int
    challenge_name: str
    flag: str
    status: str = FlagStatus.UNKNOWN
    timestamp: str = ''


class NormalFormatter(logging.Formatter):
    @override
    def format(self, record: logging.LogRecord) -> str:
        levelname = f'{record.levelname:<8}'  # Pad to width 8
        record.levelname = f'{levelname}'
        return super().format(record)


class ColoredFormatter(logging.Formatter):
    COLORS: dict[str, str] = {
        'DEBUG': '\033[36m',  # Cyan
        'INFO': '\033[32m',  # Green
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',  # Red
        'CRITICAL': '\033[41m',  # Red background
    }
    RESET: str = '\033[0m'

    @override
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

    log_fmt = '[%(asctime)s] - %(levelname)s - %(message)s'
    datefmt = '%Y-%m-%d %H:%M:%S'

    ch = logging.StreamHandler()
    ch.setFormatter(ColoredFormatter(log_fmt, datefmt=datefmt))
    logger.addHandler(ch)

    fh = RotatingFileHandler(
        f'{LOGS_PATH}/{name}{"_" + filename if filename else ""}.log',
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    fh.setFormatter(NormalFormatter(log_fmt, datefmt=datefmt))
    logger.addHandler(fh)

    return logger


def setup_database():
    with sqlite3.connect(DATABASE_PATH, timeout=8) as c:
        _ = c.execute("""
            CREATE TABLE IF NOT EXISTS flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER,
                team_name TEXT,
                challenge_id INTEGER,
                challenge_name TEXT,
                flag TEXT,
                status TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.commit()
