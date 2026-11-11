"""Compare title-based selection with the current source-based policy, offline."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from law_change_auto.docx_generator.summary_generator import _legacy_is_relevant
from law_change_auto.services.enrichment import mark_source_conflicts
from law_change_auto.services.selection import detail_from_dict, select_candidates, write_registry


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--selection-review', type=Path)
    args = p.parse_args()
    if args.output.exists():
        p.error('기존 결과를 보존합니다. 새 출력 폴더를 지정하세요.')
    raw = json.loads(args.source.read_text(encoding='utf-8'))
    details = [detail_from_dict(d) for d in raw]
    old = {id(d): _legacy_is_relevant(d) for d in details}
    mark_source_conflicts(details)
    selected = select_candidates(details, args.selection_review)
    args.output.mkdir(parents=True)
    (args.output/'input_snapshot.json').write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
    registry = write_registry(args.output, details, {})
    result = {'source': str(args.source), 'candidate_count': len(details),
              'previous_highlight_count': sum(old.values()), 'new_priority_count': len(selected),
              'counts': registry['counts'], 'external_calls': 0, 'document_generation': False,
              'legal_currentness': 'not_checked; classification of saved sources only',
              'rows': [{'no': i, 'law_name': d.meta.law_name, 'previous_highlight': old[id(d)],
                        'selection': d.selection} for i,d in enumerate(details, 1)]}
    (args.output/'selection_comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 후보 선별 기준 비교', '',
             f"기존 중요항목 {sum(old.values())}건 → 자동 우선 안내 {len(selected)}건. 전체 후보 {len(details)}건 보존.",
             '저장 원문에 대한 보수적인 자동 분류임. 실제 법률 대조·회사 적용 여부 검토·배포 승인은 별도임.', '',
             '| 번호 | 법령명 | 기존 중요항목 | 새 분류 | 사유 |', '|---|---|---|---|---|']
    for i,d in enumerate(details,1):
        a=d.selection
        lines.append(f"| {i} | {d.meta.law_name.replace('|','/')} | {'선정' if old[id(d)] else '-'} | {a['label']} | {a['reason'].replace('|','/')} |")
    (args.output/'선별기준_전후비교.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    # No pre-approved entries: these are editable suggestions, not review evidence.
    template = {'schema': 1, 'decisions': [{**{k:d.selection[k] for k in ('candidate_id','source_sha256','status','evidence')},
                                          'law_name': d.meta.law_name, 'reviewer':'', 'reason':'', 'role':'', 'change':''}
                                         for d in details if d.selection['status'] == 'conditional']}
    (args.output/'selection_review_template.json').write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='rows'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
