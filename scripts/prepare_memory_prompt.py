"""Prepare a reviewed memory snapshot; never summarize or change runtime config."""

import argparse
import json
import os
import tempfile
from pathlib import Path

from nanobot.agent.memory_prompt import make_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--summary', type=Path, required=True,
                        help='Manually reviewed short memory; retain all active constraints')
    parser.add_argument('--budget', type=int, required=True, metavar='1..20000')
    parser.add_argument('--out', type=Path, required=True,
                        help='Candidate JSON path; no config changes or automatic activation')
    args = parser.parse_args()
    if not 1 <= args.budget <= 20_000:
        parser.error('--budget must be between 1 and 20000')
    source = args.workspace.expanduser().resolve() / 'memory' / 'MEMORY.md'
    if args.out.resolve() in {source.resolve(), args.summary.resolve()}:
        parser.error('--out must not overwrite the source or reviewed summary')
    # All checks precede the write. A rejected update leaves an existing snapshot intact.
    snapshot = make_snapshot(source.read_text(encoding='utf-8'),
                             args.summary.read_text(encoding='utf-8'), source, args.budget)
    content = json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n'
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.memory-prompt-', dir=args.out.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, args.out)
    finally:
        Path(temp).unlink(missing_ok=True)
    print(f'Prepared {args.out}; original memory and runtime config unchanged.')


if __name__ == '__main__':
    main()
