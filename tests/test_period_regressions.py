from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock
import json
import xml.etree.ElementTree as ET

import pytest

from law_change_auto.models import LawChangeDetail, LawChangeMeta
from law_change_auto.config.monitored_laws_loader import MonitoredLaw
from law_change_auto.services import diagnostics

START, END = date(2026, 9, 1), date(2026, 10, 31)

def item(name='보험업법', eff=START, category='법령', change='시행'):
    return LawChangeDetail(LawChangeMeta(name, category, change, START, eff),
                           main_change_sections=['보험회사는 설명 사항을 추가함.'])

def test_pagination(monkeypatch):
    from law_change_auto.fetchers import national_law_fetcher as f
    call = Mock(side_effect=[ET.fromstring('<r><totalCnt>101</totalCnt><law>A</law></r>'),
                            ET.fromstring('<r><totalCnt>101</totalCnt><law>B</law></r>')])
    monkeypatch.setattr(f, '_request_law_search', call)
    root = f._request_all_pages({'display':'100'})
    assert [e.text for e in root.iter('law')] == ['A', 'B']
    assert call.call_args_list[1].args[0]['page'] == '2'

def test_pagination_missing_total_is_not_success(monkeypatch):
    from law_change_auto.fetchers import national_law_fetcher as f
    monkeypatch.setattr(f, '_request_law_search', lambda p: ET.fromstring('<r/>'))
    with pytest.raises(RuntimeError):
        f._request_all_pages({})

def test_admin_effective_uses_supported_sort_and_stops(monkeypatch):
    from law_change_auto.fetchers import national_law_fetcher as f
    call = Mock(side_effect=lambda p: ET.fromstring('<r><totalCnt>24155</totalCnt><admrul><시행일자>20260831</시행일자></admrul></r>'
                if p.get('sort') == 'efdes' else '<r><totalCnt>0</totalCnt></r>'))
    monkeypatch.setattr(f, '_request_law_search', call)
    f.get_recent_admin_rule_changes_range(START, END)
    assert all('efYd' not in c.args[0] for c in call.call_args_list)
    assert any(c.args[0].get('sort') == 'efdes' for c in call.call_args_list)
    assert call.call_count <= 4

def test_calendar_does_not_mislabel_old_event(monkeypatch):
    from law_change_auto.fetchers import web_scraper as f
    old = item(eff=date(2021,10,9)).meta
    old.announcement_date = date(2021,6,8)
    old.lsi_seq = '1'
    monkeypatch.setattr(f.requests, 'get', lambda *a, **k: SimpleNamespace(status_code=200, text='<table><tr></tr></table>'))
    monkeypatch.setattr(f, '_parse_calendar_row', lambda *a: old)
    result = f.fetch_calendar_laws(START)
    assert len(result) == 1
    assert result[0].change_type == '기타'
    assert result[0].calendar_event_date == START

def test_notice_zero_match_does_not_include_all(monkeypatch, tmp_path):
    from law_change_auto.services import pipeline as p
    unrelated = item('전혀무관한법', category='입법예고', change='입법예고').meta
    monkeypatch.setattr(p, 'fetch_fsc_legislation_list', lambda **k: [unrelated])
    assert p.collect_legislation_details(tmp_path, [MonitoredLaw('보험업법')], '', START, END) == []

def test_notice_title_normalization():
    from law_change_auto.matching.law_matcher import match_laws
    notice = item('「보험업법 시행령」 일부개정령안 입법예고', category='입법예고').meta
    assert len(match_laws([MonitoredLaw('보험업법')], [notice])) == 1

def test_cached_combined_draft_one_call(monkeypatch, tmp_path):
    from law_change_auto.services import enrichment as e
    call = Mock(return_value='{"summary":"보험 관련 제도 변경", "impact":"업무절차 확인 필요"}')
    def transport(*args, evidence=None, **kwargs):
        evidence.update(provider='Groq', model=e.llm.GROQ_MODEL,
                        response_id='test-response-'+str(call.call_count+1), usage={'total_tokens': 20})
        return call(*args)
    monkeypatch.setattr(e.llm, '_fetch_from_groq', transport)
    monkeypatch.setattr(e.llm, '_fetch_from_gemini', Mock(side_effect=AssertionError('unexpected fallback')))
    assert not e.enrich(item(), tmp_path, impact=True)
    again = item()
    assert e.enrich(again, tmp_path, impact=True)
    assert call.call_count == 1
    assert again.summary_text and again.impact_analysis
    changed = item(); changed.meta.lsi_seq = 'new-version'
    assert not e.enrich(changed, tmp_path, impact=True)
    assert call.call_count == 2

def test_period_policy_and_file_mapping(monkeypatch, tmp_path):
    from law_change_auto import cli
    from law_change_auto.docx_generator import summary_generator as sg
    monkeypatch.setattr(sg, 'generate_summary_docx', Mock())
    gen = Mock()
    monkeypatch.setattr(cli, 'generate_guide', gen)
    ds = [item(), item('보험업법 시행령'), item('상법', date(2027,1,1)),
          item('보험업법 개정안', category='입법예고', change='입법예고')]
    diagnostics.reset()
    paths = cli._publish_period(ds, tmp_path/'default', START, END, '기간', use_llm=False, digest_only=True)
    assert len(paths) == 1 and gen.call_count == 0
    report = json.loads((tmp_path/'default/run_report.json').read_text())
    assert all(d['output_file'] is None for d in report['details'])
    paths = cli._publish_period(ds, tmp_path/'details', START, END, '기간', use_llm=False, detailed_guides=True)
    assert len(paths) == 2 and gen.call_count == 1
    assert len(gen.call_args.args[0]) == 2
    report = json.loads((tmp_path/'details/run_report.json').read_text())
    assigned = [d['output_file'] for d in report['details'] if d['output_file']]
    assert len(assigned) == 2 and len(set(assigned)) == 1
    with pytest.raises(FileExistsError):
        cli._publish_period(ds, tmp_path/'details', START, END, '기간', use_llm=False)

def test_empty_failed_run_keeps_report(tmp_path):
    from law_change_auto import cli
    diagnostics.reset(); diagnostics.warn('test collection failed')
    assert cli._publish_period([], tmp_path, START, END, '기간', use_llm=False) == []
    report = json.loads((tmp_path/'run_report.json').read_text())
    assert report['coverage_status'] == 'incomplete'
    assert report['warnings']
    diagnostics.reset()

def test_real_docx_writer_without_network(tmp_path, monkeypatch):
    from law_change_auto import cli
    from law_change_auto.services import gemini_client
    from docx import Document
    monkeypatch.setattr(gemini_client, 'fetch_one_line_summary', Mock(side_effect=AssertionError('network not allowed')))
    diagnostics.reset()
    ds = [item(), item('보험업법 시행령'), item('상법', date(2027,1,1))]
    paths = cli._publish_period(ds, tmp_path, START, END, '2026. 9. 1. ~ 2026. 10. 31.',
                                use_llm=False, detailed_guides=True)
    assert len(paths) == 2 and all(p.exists() for p in paths)
    summary = Document(paths[-1])
    text = '\n'.join(p.text for p in summary.paragraphs)
    assert 'Ⅱ. 당사 관련성 높은 주요 개정 사항' in text
    assert len(summary.tables) == 2 and len(summary.tables[-1].rows) == 4
    guide = Document(paths[0])
    text = '\n'.join(p.text for p in guide.paragraphs)
    assert '보험업법 시행 안내' in text and '보험업법 시행령 시행 안내' in text

def test_indirect_amendment_does_not_summarize_parent_reason(tmp_path, monkeypatch):
    from law_change_auto import cli
    from law_change_auto.services import enrichment
    from law_change_auto.docx_generator import summary_generator as sg
    candidate = item(); candidate.meta.amendment_type = '타법개정'
    monkeypatch.setattr(enrichment, 'enrich', Mock(side_effect=AssertionError('must not summarize unrelated reason')))
    monkeypatch.setattr(sg, 'generate_summary_docx', Mock())
    cli._publish_period([candidate], tmp_path, START, END, '기간', use_llm=True)
    assert candidate.selection['status'] == 'withheld'
    assert '실제 변경조문 확인' in candidate.selection['reason']

def test_summary_cleaner_preserves_first_korean_syllable():
    from law_change_auto.services.gemini_client import _clean_summary
    from law_change_auto.docx_generator.summary_generator import _strip_lead
    assert _clean_summary('가상자산거래소 적용대상 확대') == '가상자산거래소 적용대상 확대'
    assert _strip_lead('가상자산거래소 적용대상 확대') == '가상자산거래소 적용대상 확대'
    assert _clean_summary('가. 가상자산거래소 적용대상 확대') == '가상자산거래소 적용대상 확대'

def test_summary_name_normalizes_halfwidth_brackets_without_mutating_source():
    from law_change_auto.docx_generator.summary_generator import _short_name
    d = item('｢금융소비자 보호에 관한 감독규정｣ 일부개정고시안', category='입법예고', change='규정변경예고')
    assert _short_name(d).startswith('「금융소비자 보호에 관한 감독규정」')
    assert d.meta.law_name.startswith('｢')
