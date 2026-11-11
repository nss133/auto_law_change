"""Replay selection against a saved source audit, with pinned comparison evidence.

No network, document generation, AI calls, or delivery-review approval occurs.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from law_change_auto.parsers.law_change_parser import _parse_old_new_table
from law_change_auto.services.enrichment import digest, source_payload
from law_change_auto.services.selection import (assess, candidate_id, detail_from_dict,
                                               select_candidates, validate_assessment, write_registry)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def restore_comparison(detail, path):
    """Only replace comparisons after matching the API's echoed new version."""
    xml = path.read_text(encoding='utf-8')
    meta = detail.meta
    header = ET.fromstring(xml).find('신조문_기본정보')
    expected = {'법령일련번호': meta.lsi_seq, '법령명': meta.law_name,
                '공포일자': meta.announcement_date.strftime('%Y%m%d'),
                '시행일자': meta.effective_date.strftime('%Y%m%d'),
                '공포번호': meta.promulgation_no or meta.law_number}
    if header is None or any(re.sub(r'\s+', '', header.findtext(k, '')) != re.sub(r'\s+', '', v or '')
                             for k, v in expected.items()):
        raise ValueError(f'Comparison version mismatch: {path.name}')
    rows = _parse_old_new_table(xml)
    if not rows:
        raise ValueError(f'Comparison missing: {path.name}')
    old_hash = digest(source_payload(detail))
    detail.article_comparisons = rows
    return {'path': str(path), 'sha256': sha256(path), 'echoed_metadata': expected,
            'candidate_id': candidate_id(detail), 'old_source_sha256': old_hash,
            'new_source_sha256': digest(source_payload(detail))}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--expectations', type=Path, required=True)
    p.add_argument('--comparison-dir', type=Path)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        p.error('기존 결과를 보존합니다. 새 출력 폴더를 지정하세요.')
    raw = json.loads(args.source.read_text(encoding='utf-8'))
    audit = json.loads(args.expectations.read_text(encoding='utf-8'))
    if audit['input_sha256'] != sha256(args.source):
        p.error('검토한 입력 파일의 해시가 다릅니다.')
    expected = {row['candidate_id']: row for row in audit['candidates']}
    details = [detail_from_dict(row) for row in raw]
    if len(expected) != len(details) or set(expected) != {candidate_id(d) for d in details}:
        p.error('검토 목록에 후보 누락 또는 중복이 있습니다.')
    original_source_results = [assess(d) for d in details]
    receipts = []
    for d in details:
        if args.comparison_dir and d.meta.lsi_seq:
            path = args.comparison_dir / f'old_new_{d.meta.lsi_seq}.xml'
            if path.exists():
                receipts.append(restore_comparison(d, path))
    select_candidates(details)
    for d in details:
        validate_assessment(d)
    rows = []
    for no, (d, before, same_source) in enumerate(zip(details, raw, original_source_results), 1):
        e = expected[candidate_id(d)]
        rows.append({'no': no, 'law_name': d.meta.law_name, 'candidate_id': candidate_id(d),
                     'before': before['selection']['status'], 'same_source_new_rules': same_source['status'],
                     'after': d.selection['status'], 'rule_id': d.selection['rule_id'],
                     'expected': e['expected_status'], 'matches_audit': d.selection['status'] == e['expected_status'],
                     'audit_reason': e['reason'], 'assessment': d.selection})
    args.output.mkdir(parents=True)
    (args.output/'input_snapshot.json').write_bytes(args.source.read_bytes())
    (args.output/'rules_snapshot.json').write_text(
        (Path(__file__).resolve().parents[1]/'data/selection_rules.json').read_text(encoding='utf-8'), encoding='utf-8')
    registry = write_registry(args.output, details, {})
    result = {'source': str(args.source), 'input_sha256': sha256(args.source),
              'expectations_sha256': sha256(args.expectations), 'candidate_count': len(details),
              'before_counts': dict(Counter(row['before'] for row in rows)),
              'same_source_new_rules_counts': dict(Counter(row['same_source_new_rules'] for row in rows)),
              'after_counts': registry['counts'], 'restored_comparisons': receipts,
              'mismatches': [row['no'] for row in rows if not row['matches_audit']],
              'external_calls': 0, 'document_generation': False, 'delivery_approval': False,
              'limits': audit['limits'], 'rows': rows}
    (args.output/'audit_results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 44건 후보 선별 대조', '',
             '저장 원문 및 별도 보존한 공식 신구조문에 대한 코드 재실행 결과임. 사람의 배포 검수 승인이 아님.', '',
             '| 번호 | 법령명 | 이전 | 같은 원문·새 규칙 | 삭제 표시 복원 후 | 원문 검토 사유 |',
             '|---|---|---|---|---|---|']
    for row in rows:
        values = [str(row[k]).replace('|', '/') for k in ('no','law_name','before','same_source_new_rules','after','audit_reason')]
        lines.append('| ' + ' | '.join(values) + ' |')
    (args.output/'후보별_대조결과.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('rows','restored_comparisons')}, ensure_ascii=False, indent=2))
    return 1 if result['mismatches'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
