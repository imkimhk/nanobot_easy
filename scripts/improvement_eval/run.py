"""Run related offline invariants; never call a provider or score model quality."""
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def check_status(node, records):
    path, name = node.split('::')
    module = path.removesuffix('.py').replace('/', '.')
    matches = [r for r in records if r.get('classname') == module
               and r.get('name', '').split('[')[0] == name]
    if not matches:
        return 'missing'
    if any(r.find('failure') is not None or r.find('error') is not None for r in matches):
        return 'failed'
    if any(r.find('skipped') is not None for r in matches):
        return 'skipped'
    return 'passed'


def validate(cases):
    if len(cases) != 24 or len({c['id'] for c in cases}) != 24:
        raise ValueError('Expected 24 unique cases')
    if Counter(c['group'] for c in cases) != dict.fromkeys(
        ['general', 'followup', 'memory', 'files', 'recovery', 'skills'], 4
    ):
        raise ValueError('Expected four cases in each of six groups')
    for case in cases:
        if not case['input'] or not case['expected']:
            raise ValueError('Input and acceptance criteria are required')
        for node in case['offline_checks']:
            path, name = node.split('::')
            if not path.startswith('tests/') or '..' in Path(path).parts:
                raise ValueError('Checks must be repository tests')
            if not name.startswith('test_'):
                raise ValueError('Expected a test function')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    manifest = Path(__file__).with_name('cases.json')
    cases = json.loads(manifest.read_text())
    validate(cases)
    nodes = sorted({node for c in cases for node in c['offline_checks']})
    with tempfile.TemporaryDirectory(prefix='nanobot-eval-') as temp:
        junit = Path(temp) / 'results.xml'
        result = subprocess.run(
            [sys.executable, '-m', 'pytest', *nodes, '-q', '--tb=short',
             f'--junitxml={junit}'], cwd=ROOT, timeout=180, check=False,
        )
        records = list(ET.parse(junit).iter('testcase')) if junit.exists() else []
    for case in cases:
        case['offline_results'] = {node: check_status(node, records)
                                   for node in case['offline_checks']}
        case['model_status'] = 'not_run'
        case['model_metrics'] = None
    source_paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    source_hashes = {}
    for name in source_paths:
        path = ROOT / name
        if path.is_file() and path.suffix in {'.py', '.md', '.toml'}:
            source_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    report = dict(
        created_at=datetime.now(UTC).isoformat(),
        scope='offline related invariants; scenario prompts are NOT sent to a model',
        commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
        worktree_status=subprocess.check_output(['git', 'status', '--short'], cwd=ROOT).decode(),
        manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        source_sha256=source_hashes, pytest_exit_code=result.returncode,
        test_count=len(records), cases=cases,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f'Report: {args.out}; model scenarios executed: 0/24')
    statuses = [v for c in cases for v in c['offline_results'].values()]
    return 0 if result.returncode == 0 and all(s == 'passed' for s in statuses) else 1


if __name__ == '__main__':
    raise SystemExit(main())
