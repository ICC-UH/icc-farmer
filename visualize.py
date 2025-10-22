import argparse
import sqlite3
from collections import Counter

from shared import Flag


def main():
    parser = argparse.ArgumentParser(description='List flags with simple output.')
    parser.add_argument(
        '--sort', choices=['team', 'challenge', 'status'], help='Sort by field'
    )
    parser.add_argument('--filter-status', help='Show only flags with this status')
    args = parser.parse_args()

    conn = sqlite3.connect('flags.db', timeout=10)
    cursor = conn.cursor()

    cursor.execute(
        'SELECT team_id, team_name, challenge_id, challenge_name, flag, status, timestamp FROM flags'
    )
    rows = cursor.fetchall()

    flags = [
        Flag(
            team_id=row[0],
            team_name=row[1],
            challenge_id=row[2],
            challenge_name=row[3],
            flag=row[4],
            status=row[5],
            timestamp=row[6],
        )
        for row in rows
    ]

    # Filtering
    if args.filter_status:
        flags = [f for f in flags if f.status.lower() == args.filter_status.lower()]

    # Sorting
    if args.sort == 'team':
        flags.sort(key=lambda f: f.team_name)
    elif args.sort == 'challenge':
        flags.sort(key=lambda f: f.challenge_name)
    elif args.sort == 'status':
        flags.sort(key=lambda f: f.status)
    elif args.sort == 'timestamp':
        flags.sort(key=lambda f: f.timestamp)

    # Prepare data for simple table
    headers = [
        'Team ID',
        'Team Name',
        'Challenge ID',
        'Challenge Name',
        'Flag',
        'Status',
        'Timestamp'
    ]
    col_widths = [len(h) for h in headers]
    for f in flags:
        values = [
            str(f.team_id),
            f.team_name,
            str(f.challenge_id),
            f.challenge_name,
            f.flag,
            f.status,
            f.timestamp
        ]
        col_widths = [max(w, len(v)) for w, v in zip(col_widths, values)]

    def format_row(row):
        return ' | '.join(str(v).ljust(w) for v, w in zip(row, col_widths))

    print(format_row(headers))
    print('-+-'.join('-' * w for w in col_widths))
    for f in flags:
        row = [
            str(f.team_id),
            f.team_name,
            str(f.challenge_id),
            f.challenge_name,
            f.flag,
            f.status,
            f.timestamp
        ]
        print(format_row(row))

    # Summary
    print('\nSummary:')
    print(f'Total flags: {len(flags)}')
    status_counts = Counter(f.status for f in flags)
    for status, count in status_counts.items():
        print(f'{status}: {count}')

    conn.close()


if __name__ == '__main__':
    main()
