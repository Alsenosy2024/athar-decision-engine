#!/usr/bin/env python3
"""Run and replay the H3 benchmark. Emit its audit ledger as JSON."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.decision_loop.benchmark import run_benchmark


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Write the ledger here instead of stdout.')
    args = parser.parse_args()
    report = run_benchmark()
    output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if args.output:
        args.output.write_text(output, encoding='utf-8')
    else:
        sys.stdout.write(output)
    replay = report['replay']
    if (replay['evaluation_matched'] != replay['evaluation_windows']
        or replay['training_matched'] != replay['training_windows']
        or not replay['coefficients_matched']):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
