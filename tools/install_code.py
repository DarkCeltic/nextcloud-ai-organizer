#!/usr/bin/env python3
"""Install complete project-source replacements; never touch config, secrets or SQLite."""
from __future__ import annotations
import argparse
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    'appinfo/info.xml', 'Dockerfile', 'requirements.txt',
    'exapp/__init__.py', 'exapp/automation.py', 'exapp/main.py',
    'exapp/routes/__init__.py', 'exapp/routes/analyze.py', 'exapp/routes/apply.py',
    'exapp/routes/dashboard.py', 'exapp/routes/file_action.py', 'exapp/routes/settings.py',
    'exapp/static/app.js', 'exapp/static/app.css',
    'python_organizer_local_llm/__init__.py',
    'python_organizer_local_llm/classifier.py', 'python_organizer_local_llm/database.py',
    'python_organizer_local_llm/nextcloud.py', 'python_organizer_local_llm/ollama.py',
    'python_organizer_local_llm/organizer.py', 'python_organizer_local_llm/scanner.py',
    'python_organizer_local_llm/sensitive.py',
    'python_organizer_local_llm/settings.py',
    'python_organizer_local_llm/file_types.py',
    'python_organizer_local_llm/ocr.py',
    'python_organizer_local_llm/suggestion_policy.py',
)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True,
                        help='Existing AI Organizer project root (containing exapp/ and config.yaml).')
    args = parser.parse_args()
    project = args.project.expanduser().resolve()
    if not (project / 'exapp').is_dir() or not (project / 'config.yaml').is_file():
        parser.error('Target must be existing project root with exapp/ and config.yaml.')
    if project == ROOT:
        parser.error('Extract release outside the project and pass the existing project as --project.')
    missing = [f for f in FILES if not (ROOT / f).is_file()]
    if missing:
        parser.error(f'Release incomplete; missing source files: {missing}')
    backup = project.parent / f'{project.name}_code_backups' / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    for rel in FILES:
        existing = project / rel
        if existing.is_file():
            dest = backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(existing, dest)
    for rel in FILES:
        dest = project / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    print(f'Installed {len(FILES)} complete source files into {project}')
    print(f'Previous code backup: {backup}')
    print('Configuration, .env, compose.yaml, SQLite and data directories were NOT modified.')
    print('The running ExApp must be stopped before deployment; restart/rebuild only your existing instance.')

if __name__ == '__main__':
    main()
