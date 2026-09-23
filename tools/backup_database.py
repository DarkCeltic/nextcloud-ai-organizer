"""Create a consistent SQLite backup using the path from the actual config.yaml."""
import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.yaml', type=Path)
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text(encoding='utf-8')) or {}
    path = Path(config.get('database', {}).get('path', '/data/python_organizer_local_llm.db'))
    if not path.is_file():
        raise FileNotFoundError(f'Refusing to create an empty database. No file at: {path}')
    destination = path.with_name(path.name + '.' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.backup')
    with sqlite3.connect(str(path)) as source:
        with sqlite3.connect(str(destination)) as target:
            source.backup(target)
            result = target.execute('PRAGMA integrity_check').fetchone()[0]
            if result != 'ok':
                raise RuntimeError(f'Backup failed integrity check: {result}')
    print(f'Original database: {path.resolve()}')
    print(f'Backup database:   {destination.resolve()}')
    print('Backup integrity: ok')


if __name__ == '__main__':
    main()
