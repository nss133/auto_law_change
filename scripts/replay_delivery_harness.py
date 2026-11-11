"""Replay the incident snapshot offline; draft completeness and fail-closed release only."""
import argparse
from datetime import date
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from law_change_auto.cli import _publish_period
from law_change_auto.models import LawChangeMeta, LawChangeDetail, ArticleComparisonRow
from law_change_auto.docx_generator.reference import validate_reference
from law_change_auto.services.delivery import package_delivery
from law_change_auto.services.diagnostics import reset


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--date-from',type=date.fromisoformat,required=True)
    p.add_argument('--date-to',type=date.fromisoformat,required=True)
    args=p.parse_args()
    details=[]
    for raw in json.loads(args.source.read_text()):
        for k in ('announcement_date','effective_date','calendar_event_date'):
            if raw['meta'].get(k):raw['meta'][k]=date.fromisoformat(raw['meta'][k])
        raw['meta']=LawChangeMeta(**raw['meta'])
        raw['article_comparisons']=[ArticleComparisonRow(**r) for r in raw['article_comparisons']]
        details.append(LawChangeDetail(**raw))
    reset()
    with patch('requests.sessions.Session.request',side_effect=AssertionError('No external calls allowed')):
        files=_publish_period(details,args.output,args.date_from,args.date_to,
                              f'{args.date_from} ~ {args.date_to}',use_llm=False)
    report=json.loads((args.output/'run_report.json').read_text())
    manifest=json.loads((args.output/'delivery_manifest.json').read_text())
    selected = report['summary']['selected_count']
    assert report['summary']['individual_guide_count']==selected
    assert len({d['output_file'] for d in manifest['details']})==selected
    assert len(json.loads((args.output/'candidate_selection.json').read_text())['candidates']) == len(details)
    for f in manifest['files']:
        if f['kind'] in ('guide','toc'):validate_reference(args.output/f['name'])
    target=args.output/'must_not_publish.zip'
    try:
        package_delivery(args.output,target)
    except ValueError:
        pass
    else:
        raise AssertionError('Unreviewed run must not ship')
    assert not target.exists()
    result={'snapshot':str(args.source.resolve()),'detected_candidates':len(details),'individual_guides':selected,
            'generated_docx':len(files),'reference_structure':'passed',
            'api_calls':0,'network':'denied','release_without_review':'blocked',
            'visual_review':'not_performed; these files are regression drafts, not deliverables'}
    (args.output/'harness_regression_report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
