"""Regressions for the 2026-10-26 missing guides/template/LLM bypass incident."""
from datetime import date
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import json

import pytest
from docx import Document

from law_change_auto import cli
from law_change_auto.models import LawChangeDetail, LawChangeMeta, ArticleComparisonRow

START, END = date(2026, 10, 1), date(2026, 11, 30)


def item(name='보험업법', category='법령', effective=START):
    return LawChangeDetail(LawChangeMeta(name, category, '시행', START, effective,
        source='https://www.law.go.kr/test', lsi_seq=name),
                           reason_sections=['개정이유 원문임.'], main_change_sections=['보험회사는 설명 사항을 추가함.'])


def test_default_period_keeps_every_selected_version_individually(tmp_path):
    ds = [item(), item('보험업법 시행령'), item('향후법', effective=date(2027, 1, 1)),
          item('예고안', category='입법예고')]
    cli._publish_period(ds, tmp_path, START, END, '기간', use_llm=False)
    report = json.loads((tmp_path/'run_report.json').read_text())
    assert report['publication_policy'] == 'individual-and-digest'
    assert report['summary']['individual_guide_count'] == 4
    assert len({d['output_file'] for d in report['details']}) == 4
    assert (tmp_path/'목차.docx').exists()
    assert report['delivery_status'] != 'ready'


def test_digest_only_is_explicit(tmp_path):
    cli._publish_period([item()], tmp_path, START, END, '기간', use_llm=False, digest_only=True)
    report = json.loads((tmp_path/'run_report.json').read_text())
    assert report['publication_policy'] == 'digest-only'
    assert report['summary']['individual_guide_count'] == 0


def test_reference_package_and_roles_survive_notice_generation(tmp_path):
    from law_change_auto.docx_generator.reference import reference_path, validate_reference
    d = item('보험업법 일부개정안', category='입법예고'); d.opinion_deadline='2026. 11. 1.'
    p=tmp_path/'guide.docx'; cli.generate_guide([d], START, p)
    validate_reference(p)
    with ZipFile(reference_path()) as a, ZipFile(p) as b:
        assert all(a.read(n)==b.read(n) for n in a.namelist() if n!='word/document.xml')
    doc=Document(p)
    assert doc.paragraphs[0].style.name == 'Normal'
    assert doc.paragraphs[0].runs[0].font.name == 'KoPub돋움체_Pro Bold'
    assert '3. 의견제출기한' in [p.text for p in doc.paragraphs]
    assert '4. 파급효과' in [p.text for p in doc.paragraphs]


def test_unproven_injected_impact_is_never_rendered(tmp_path):
    d=item(); d.impact_analysis='보험금 지급심사 전면 변경이 필요함.'
    p=tmp_path/'guide.docx';cli.generate_guide([d], START, p)
    text='\n'.join(p.text for p in Document(p).paragraphs)
    assert d.impact_analysis not in text
    assert '호출 증거' in text


def test_unproven_cache_cannot_count_as_llm_success(monkeypatch,tmp_path):
    from law_change_auto.services import enrichment as e
    from law_change_auto.services import gemini_client as llm
    monkeypatch.setattr(llm,'_fetch_from_groq',lambda *a,**k:'{"summary":"요약", "impact":"근거 없는 영향임."}')
    monkeypatch.setattr(llm,'_fetch_from_gemini',lambda *a,**k:None)
    d=item();e.enrich(d,tmp_path,impact=True)
    assert not d.impact_analysis
    assert d.impact_evidence['status']=='failed'
    assert not list(tmp_path.glob('*.json'))


def test_source_context_includes_diff_exceptions_and_future_date():
    from law_change_auto.services.enrichment import build_draft_prompt
    d=item(effective=date(2027,4,21)); d.main_change_sections=['가'*7000]
    d.article_comparisons=[ArticleComparisonRow('제1조',None,'기존 의무','영업비밀 예외로 제공하지 않을 수 있음.')]
    prompt=build_draft_prompt(d,impact=True)
    assert '생명보험회사' in prompt and '2027-04-21' in prompt
    assert '영업비밀 예외' in prompt and '주체' in prompt
    assert '공보험' in prompt and '민영보험' in prompt


def test_legacy_style_mutation_fails_reference_gate(tmp_path):
    from law_change_auto.docx_generator.reference import reference_path,validate_reference
    p=tmp_path/'bad.docx'
    with ZipFile(reference_path()) as a,ZipFile(p,'w',ZIP_DEFLATED) as b:
        for name in a.namelist():
            raw=a.read(name)
            if name=='word/styles.xml':raw=raw.replace(b'KoPub',b'Other')
            b.writestr(name,raw)
    with pytest.raises(ValueError):validate_reference(p)


def test_release_refuses_unreviewed_artifacts_and_writes_no_zip(tmp_path):
    from law_change_auto.services.delivery import package_delivery
    cli._publish_period([item()],tmp_path/'run',START,END,'기간',use_llm=False)
    target=tmp_path/'ready.zip'
    with pytest.raises(ValueError):package_delivery(tmp_path/'run',target)
    assert not target.exists()


def test_body_only_font_and_section_drift_is_blocked(tmp_path):
    from law_change_auto.docx_generator.reference import validate_reference
    from lxml import etree as E
    d=item(); p=tmp_path/'good.docx';cli.generate_guide([d],START,p)
    with ZipFile(p) as z:
        parts={n:z.read(n) for n in z.namelist()}
    bad=tmp_path/'bad.docx'
    parts['word/document.xml']=parts['word/document.xml'].replace('1. 개정이유'.encode(),'1. 핵심요약'.encode())
    with ZipFile(bad,'w',ZIP_DEFLATED) as z:
        for n,raw in parts.items():z.writestr(n,raw)
    with pytest.raises(ValueError):validate_reference(bad)


@pytest.fixture
def verified_transport(monkeypatch):
    from law_change_auto.services import gemini_client as llm
    from types import SimpleNamespace
    calls=[]
    def post(*args,**kwargs):
        calls.append(kwargs['json'])
        return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{
            'id':f'mocked-response-{len(calls)}','model':llm.GROQ_MODEL,
            'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150},
            'choices':[{'message':{'content':json.dumps({'summary':'회사 적용 범위 확인 필요함.',
                                                       'impact':'적용대상에 해당하는 경우 개정된 절차를 확인할 필요가 있음.'},ensure_ascii=False)}}]})
    monkeypatch.setattr(llm,'_get_groq_key',lambda:'test-only-key')
    monkeypatch.setattr(llm,'_groq_throttle',lambda:None)
    monkeypatch.setattr(llm.requests,'post',post)
    return calls


def test_transport_evidence_cache_and_diff_change(verified_transport,tmp_path):
    from law_change_auto.services.enrichment import enrich,valid_evidence
    d=item(); assert not enrich(d,tmp_path,impact=True)
    assert valid_evidence(d) and len(verified_transport)==1
    assert d.impact_evidence['response_id']=='mocked-response-1'
    assert enrich(item(),tmp_path,impact=True) and len(verified_transport)==1
    changed=item();changed.article_comparisons=[ArticleComparisonRow('제2조',None,'원문','새로운 예외')]
    assert not enrich(changed,tmp_path,impact=True) and len(verified_transport)==2
    changed.article_comparisons[0].new_text='예외 변경'
    assert not valid_evidence(changed)


def test_editorial_correction_preserves_api_original(verified_transport,tmp_path):
    from law_change_auto.services.enrichment import enrich,valid_evidence,set_reviewed_impact,digest
    d=item();enrich(d,tmp_path,impact=True); original=d.impact_evidence['raw_response']
    set_reviewed_impact(d,'자료제공의 주체는 국가기관이므로 당사의 직접 의무는 확인되지 않음.',
                        reviewer='Codex test fixture',reason='국가기관과 회사의 의무 주체를 분리함.')
    assert valid_evidence(d) and d.impact_evidence['raw_response']==original
    d.impact_analysis='의무를 임의 추가함.';d.impact_evidence['impact_sha256']=digest(d.impact_analysis)
    assert not valid_evidence(d)


def test_cache_output_tampering_does_not_survive(verified_transport,tmp_path):
    from law_change_auto.services.enrichment import enrich
    d=item();enrich(d,tmp_path,impact=True)
    p=next(tmp_path.glob('*.json')); data=json.loads(p.read_text());data['impact']='임의 주입 문구'
    p.write_text(json.dumps(data))
    again=item();assert not enrich(again,tmp_path,impact=True)
    assert again.impact_analysis!='임의 주입 문구' and len(verified_transport)==2


def test_oversize_source_is_withheld_without_truncation(monkeypatch,tmp_path):
    from law_change_auto.services import enrichment as e
    monkeypatch.setattr(e.llm,'_fetch_from_groq',lambda *a,**k:pytest.fail('must not call'))
    monkeypatch.setattr(e.llm,'_fetch_from_gemini',lambda *a,**k:pytest.fail('must not call'))
    d=item();d.main_change_sections=['가'*50000];d.impact_analysis='stale impact'
    e.enrich(d,tmp_path,impact=True)
    assert d.impact_analysis is None and d.impact_evidence['reason']=='input_limit'


def test_failed_transport_never_logs_api_query_key(monkeypatch,capsys):
    from law_change_auto.services import gemini_client as llm
    import requests
    monkeypatch.setattr(llm,'_get_gemini_key',lambda:'test-secret-never-log')
    monkeypatch.setattr(llm,'_gemini_disabled',False)
    monkeypatch.setattr(llm,'_gemini_throttle',lambda:None)
    def failed(*args,**kwargs):
        raise requests.ConnectionError('https://api.example/?key=test-secret-never-log')
    monkeypatch.setattr(llm.requests,'post',failed)
    assert llm._fetch_from_gemini('테스트','prompt') is None
    assert 'test-secret-never-log' not in capsys.readouterr().out


def test_successful_release_requires_current_semantic_and_all_page_evidence(verified_transport,tmp_path):
    """Synthetic PDF/PNG and review fixtures exercise the gate, not real visual QA."""
    from law_change_auto.services.delivery import (review_template,file_hash,validate_delivery,package_delivery)
    import fitz
    run=tmp_path/'run';cli._publish_period([item()],run,START,END,'기간')
    manifest=json.loads((run/'delivery_manifest.json').read_text())
    review=review_template(run);qa=run/'qa';qa.mkdir()
    review['selection_review'].update(status='passed', reviewer='automated test fixture', notes='Synthetic source and selection review')
    for i,(name,r) in enumerate(review['files'].items()):
        r['semantic'].update(status='passed',reviewer='automated test fixture',notes='Test source-scope review fixture')
        r['semantic']['checks']={k:True for k in r['semantic']['checks']}
        pdf=qa/f'{i}.pdf';png=qa/f'{i}.png'
        d=fitz.open();page=d.new_page();page.insert_text((72,72),'Synthetic rendering fixture')
        page.get_pixmap().save(png);d.save(pdf);d.close()
        r['visual'].update(status='passed',reviewer='automated test fixture',rendered_from_sha256=r['sha256'],
            pdf=str(pdf.relative_to(run)),pdf_sha256=file_hash(pdf),pages_reviewed=1,
            images=[{'name':str(png.relative_to(run)),'sha256':file_hash(png)}])
    (run/'delivery_review.json').write_text(json.dumps(review))
    assert validate_delivery(run)['status']=='ready'
    archive=package_delivery(run,tmp_path/'pass.zip');assert archive.exists()
    with ZipFile(archive) as z:
        assert z.testzip() is None
        assert not any('.env' in n or n.startswith('qa/') for n in z.namelist())
    png.write_bytes(png.read_bytes()+b'changed')
    assert validate_delivery(run)['status']=='blocked'
    with pytest.raises(ValueError):package_delivery(run,tmp_path/'changed.zip')
    assert not (tmp_path/'changed.zip').exists()


def test_partial_visual_review_cannot_ship(verified_transport,tmp_path):
    from law_change_auto.services.delivery import review_template,validate_delivery
    run=tmp_path/'run';cli._publish_period([item()],run,START,END,'기간')
    review=review_template(run)
    for r in review['files'].values():
        r['visual'].update(status='passed',reviewer='test fixture',pages_reviewed=1)
    (run/'delivery_review.json').write_text(json.dumps(review))
    result=validate_delivery(run)
    assert result['status']=='blocked'
    assert any('원문 대조' in e for e in result['errors'])


def test_tampered_artifact_invalidates_prior_review(verified_transport,tmp_path):
    from law_change_auto.services.delivery import validate_delivery
    cli._publish_period([item()],tmp_path,START,END,'기간')
    manifest=json.loads((tmp_path/'delivery_manifest.json').read_text())
    p=tmp_path/manifest['details'][0]['output_file'];p.write_bytes(p.read_bytes()+b'changed')
    assert any('해시 불일치' in e for e in validate_delivery(tmp_path)['errors'])


def test_identical_unrelated_source_explanations_are_withheld():
    from law_change_auto.services.enrichment import mark_source_conflicts,source_limit
    ds=[item('금융기관검사및제재에관한규정'),item('신용정보업감독규정')]
    for d in ds:d.reason_sections=['동일한 개정이유가 서로 다른 규정에 잘못 제공될 수 있는 원문 사례임.']
    mark_source_conflicts(ds)
    assert all('충돌' in source_limit(d) for d in ds)
    family=[item('보험업법'),item('보험업법 시행령')]
    for d in family:d.reason_sections=['같은 법령군에서 공유하는 내용이 반복되는 정상 원문 사례임.']
    mark_source_conflicts(family)
    assert not any(source_limit(d) for d in family)


def test_incomplete_period_is_not_silently_run_as_today():
    with pytest.raises(SystemExit):cli.parse_args(['--date-from','2026-10-01'])
    with pytest.raises(SystemExit):cli.parse_args(['--digest-only'])
    with pytest.raises(SystemExit):cli.parse_args(['--date-from','2026-10-01','--date-to','2026-11-30',
                                                '--digest-only','--detailed-guides'])


def test_reference_table_rows_remain_readable_and_changed_markers_preserved(tmp_path):
    from law_change_auto.docx_generator.reference import validate_reference
    d=item();d.article_comparisons=[ArticleComparisonRow('제1조',None,'종전','개정',
        old_segments=[('종전','del')],new_segments=[('개정','ins')])]
    p=tmp_path/'table.docx';cli.generate_guide([d],START,p);validate_reference(p)
    cells=Document(p).tables[0].rows[1].cells
    assert cells[0].text=='종전' and cells[1].text=='개정'
    left=cells[0].paragraphs[0].runs[0];right=cells[1].paragraphs[0].runs[0]
    assert left.font.size.pt==9 and left.underline and str(left.font.color.rgb)=='FF0000'
    assert right.font.size.pt==9 and str(right.font.color.rgb)=='FF0000'


def test_single_day_refuses_existing_output_before_collection(monkeypatch,tmp_path):
    (tmp_path/'existing.docx').write_bytes(b'original')
    monkeypatch.setattr(cli,'collect_details_for_date',lambda *a,**k:pytest.fail('must stop before collection'))
    with pytest.raises(FileExistsError):cli._process_single_date(START,tmp_path,[],'')
    assert (tmp_path/'existing.docx').read_bytes()==b'original'


def test_summary_renderer_never_retries_llm(monkeypatch,tmp_path):
    from law_change_auto.docx_generator.summary_generator import generate_summary_docx
    from law_change_auto.services import gemini_client as llm
    monkeypatch.setattr(llm,'fetch_one_line_summary',lambda *a,**k:pytest.fail('renderer must not call API'))
    assert generate_summary_docx([item()],tmp_path/'summary.docx',period_line='기간',guide_date=START,use_llm=True)


def test_empty_daily_collection_does_not_emit_fake_example(monkeypatch,tmp_path):
    monkeypatch.setattr(cli,'collect_details_for_date',lambda *a,**k:[])
    monkeypatch.setattr(cli,'collect_legislation_details',lambda *a,**k:[])
    assert cli._process_single_date(START,tmp_path,[],'')==[]
    assert not list(tmp_path.glob('*.docx'))


def test_missing_referenced_attachment_is_not_silently_omitted(tmp_path):
    from law_change_auto.services.delivery import validate_delivery
    d=item('예고안',category='입법예고');d.comparison_pdf_paths=[('개정안','missing.pdf')]
    cli._publish_period([d],tmp_path,START,END,'기간',use_llm=False)
    manifest=json.loads((tmp_path/'delivery_manifest.json').read_text())
    assert any(f['name']=='missing.pdf' and f['sha256'] is None for f in manifest['files'])
    assert any('missing.pdf' in e for e in validate_delivery(tmp_path)['errors'])
