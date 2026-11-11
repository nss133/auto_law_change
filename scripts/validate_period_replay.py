"""Replay a collected snapshot without external calls; record acceptance evidence."""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from law_change_auto.cli import _publish_period
from law_change_auto.models import LawChangeMeta, LawChangeDetail, ArticleComparisonRow
from law_change_auto.services.diagnostics import reset


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    args = parser.parse_args()
    rows = json.loads((args.source / 'collected_details.json').read_text())
    details = []
    for row in rows:
        for key in ('announcement_date', 'effective_date', 'calendar_event_date'):
            if row['meta'].get(key):
                row['meta'][key] = date.fromisoformat(row['meta'][key])
        row['meta'] = LawChangeMeta(**row['meta'])
        row['article_comparisons'] = [ArticleComparisonRow(**r) for r in row['article_comparisons']]
        details.append(LawChangeDetail(**row))
    reset()
    begun = time.perf_counter()
    # A cache miss must fail visibly, not quietly start another API run.
    with patch('requests.post', side_effect=AssertionError('Replay must not call external AI')):
        paths = _publish_period(details, args.output, date(2026,9,1), date(2026,10,31),
                                '2026. 9. 1. ~ 2026. 10. 31.', use_llm=True, digest_only=True)
    elapsed = time.perf_counter() - begun
    current = json.loads((args.output / 'run_report.json').read_text())
    live = json.loads((args.source / 'run_report.json').read_text())
    old = json.loads(args.baseline.read_text())
    def key(d):
        return d['category'], d.get('lsi_seq') or d.get('admrul_seq') or d.get('law_id')
    old_keys, new_keys = {key(d) for d in old['details']}, {key(d) for d in current['details']}
    evidence = {
        'live_source': str(args.source.resolve()), 'live_timings': live['timings'],
        'live_warnings': live['warnings'], 'replay_seconds': round(elapsed,3),
        'cache_hits': current['draft_cache_hits'], 'replay_warnings': current['warnings'],
        'old_detail_count': len(old['details']), 'new_detail_count': len(current['details']),
        'status_counts': dict(Counter(d['publication_status'] for d in current['details'])),
        'effective_family_count': current['summary']['effective_family_count'],
        'added': [d['law_name'] for d in current['details'] if key(d) not in old_keys],
        'removed': [d['law_name'] for d in old['details'] if key(d) not in new_keys],
        'indirect_amendments_requiring_review': [d.meta.law_name for d in details if '타법' in (d.meta.amendment_type or '')],
        'files_exist': all(p.exists() for p in paths),
        'visual_review': 'pending',
    }
    (args.output / 'validation_report.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
