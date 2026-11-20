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


def validate(cases, suite='full'):
    if suite == 'full':
        count = 24
        groups = dict.fromkeys(['general', 'followup', 'memory', 'files', 'recovery', 'skills'], 4)
    elif suite == 'core':
        count = 8
        groups = dict.fromkeys(
            ['simple', 'research', 'read', 'write', 'approval', 'untrusted', 'memory', 'delivery'], 1
        )
    else:
        raise ValueError(f'Unknown suite: {suite}')
    if len(cases) != count or len({c['id'] for c in cases}) != count:
        raise ValueError(f'Expected {count} unique cases')
    if Counter(c['group'] for c in cases) != groups:
        raise ValueError(f'Expected fixed groups for {suite}: {groups}')
    for case in cases:
        if not case['input'] or not case['expected']:
            raise ValueError('Input and acceptance criteria are required')
        if suite == 'core' and not case['offline_checks']:
            raise ValueError('Core cases require related offline checks')
        for node in case['offline_checks']:
            path, name = node.split('::')
            if not path.startswith('tests/') or '..' in Path(path).parts:
                raise ValueError('Checks must be repository tests')
            if not name.startswith('test_'):
                raise ValueError('Expected a test function')


def case_status(results):
    if not results:
        return 'uncovered'
    for status in ('failed', 'missing', 'skipped'):
        if status in results.values():
            return status
    return 'passed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--suite', choices=['full', 'core'], default='full',
                        help='core: eight routine checks; full: existing 24-case manifest')
    args = parser.parse_args()
    manifest = Path(__file__).with_name('core_cases.json' if args.suite == 'core' else 'cases.json')
    cases = json.loads(manifest.read_text(encoding='utf-8'))
    validate(cases, args.suite)
    nodes = sorted({node for c in cases for node in c['offline_checks']})
    if not nodes:
        raise ValueError('No checks selected; refusing to run the entire test suite')
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
        case['offline_status'] = case_status(case['offline_results'])
        case['model_status'] = 'not_run'
        case['model_metrics'] = None
    summary = dict(
        case_count=len(cases),
        offline_case_counts=dict(Counter(case['offline_status'] for case in cases)),
        linked_check_count=len(nodes),
        model_scenarios_executed=0,
    )
    source_paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    source_hashes = {}
    for name in source_paths:
        path = ROOT / name
        if path.is_file() and path.suffix in {'.py', '.md', '.toml'}:
            source_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    report = dict(
        created_at=datetime.now(UTC).isoformat(),
        scope='offline related invariants; scenario prompts are NOT sent to a model',
        suite=args.suite, manifest=manifest.name, summary=summary,
        commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
        worktree_status=subprocess.check_output(['git', 'status', '--short'], cwd=ROOT).decode(),
        manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        source_sha256=source_hashes, pytest_exit_code=result.returncode,
        test_count=len(records), cases=cases,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for case in cases:
        print(f"{case['id']} [{case['group']}]: offline={case['offline_status']}; model=not_run")
    counts = ', '.join(f'{status}={count}' for status, count in summary['offline_case_counts'].items())
    print(f'Offline cases: {counts}; pytest tests: {len(records)}')
    print(f'Report: {args.out}; model scenarios executed: 0/{len(cases)}')
    statuses = [v for c in cases for v in c['offline_results'].values()]
    return 0 if result.returncode == 0 and all(s == 'passed' for s in statuses) else 1


if __name__ == '__main__':
    raise SystemExit(main())
