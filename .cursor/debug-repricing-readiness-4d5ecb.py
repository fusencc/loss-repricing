"""Offline check. Run with the project's Python from the worker's checkout and environment."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

LOG = Path('/home/developer/projects/loss-repricing/.cursor/debug-4d5ecb.log')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--project-root', type=Path, default=LOG.parents[1])
parser.add_argument('--production', action='store_true', help='Allow a read-only query of the explicitly selected production database')
parser.add_argument('--stdout', action='store_true', help='Print only the sanitized evidence; do not write a log file')
args = parser.parse_args()
root = args.project_root.resolve()
sys.path.insert(0, str(root / 'backend'))


def record(hypothesis, message, data):
    # region agent log
    entry = json.dumps(dict(sessionId='4d5ecb', runId='production' if args.production else 'local',
                            hypothesisId=hypothesis, location='debug-repricing-readiness-4d5ecb.py',
                            message=message, data=data, timestamp=time.time_ns() // 1_000_000))
    if args.stdout:
        print(entry)
    else:
        with LOG.open('a', encoding='utf-8') as log:
            log.write(entry + '\n')
    # endregion


def main():
    from app.config import get_settings
    from app.services.store import DEFAULTS
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    try:
        settings = get_settings()
        database = make_url(settings.database_url)
    except Exception as exc:
        record('A/B/C/D', 'settings_load_failed', {'error_type': type(exc).__name__})
        return

    default_path = root / 'deploy' / 'live-contract.json'
    configured = settings.live_contract_path
    path = configured.resolve()
    try:
        contract = json.loads(path.read_text(encoding='utf-8-sig'))
        status = 'verified' if isinstance(contract, dict) and contract.get('verified') is True else 'not_verified'
    except FileNotFoundError:
        status = 'missing'
    except (OSError, ValueError) as exc:
        status = 'invalid_json' if isinstance(exc, ValueError) else type(exc).__name__
    record('B/C/D', 'effective_contract_file', {
        'status': status, 'uses_default_path': path == default_path.resolve(),
        'relative_path': not configured.is_absolute(), 'live_submit_enabled': settings.live_submit_enabled,
        'mode': settings.adapter_mode,
    })

    statement = "SELECT value FROM settings WHERE `key` = 'runtime'"
    engine = None
    try:
        if database.get_backend_name() == 'sqlite':
            if not database.database or database.database == ':memory:':
                record('A', 'database_not_persistent', {})
                return
            sqlite_path = Path(database.database).resolve()
            if not args.production and not sqlite_path.is_relative_to(root):
                record('A', 'database_outside_project_not_queried', {})
                return
            with sqlite3.connect(sqlite_path.as_uri() + '?mode=ro', uri=True) as connection:
                row = connection.execute(statement).fetchone()
        elif args.production and database.get_backend_name() == 'mysql':
            engine = create_engine(database, connect_args={'connect_timeout': 5})
            with engine.connect() as connection:
                row = connection.execute(text(statement)).fetchone()
        else:
            record('A', 'nonlocal_database_not_queried', {})
            return
        raw = row[0] if row else {}
        stored = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        effective = {**DEFAULTS, **stored}
        record('A', 'runtime_settings_read', {
            'autonomous_enabled': effective['autonomous_enabled'], 'schedule_enabled': effective['schedule_enabled'],
            'stored_explicitly': 'autonomous_enabled' in stored,
            'pilot_shop_count': len(effective['pilot_shops']), 'enabled_site_count': len(effective['site_enabled']),
        })
    except Exception as exc:
        record('A', 'database_read_failed', {'error_type': type(exc).__name__})
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    main()
