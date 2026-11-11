"""Selection behavior and delivery boundaries; all sources and transports are synthetic."""
from datetime import date
import json
from unittest.mock import Mock

import pytest

from law_change_auto import cli
from law_change_auto.models import LawChangeDetail, LawChangeMeta
from law_change_auto.services.selection import assess, apply_review


def item(body='', name='보험업법', category='법령'):
    return LawChangeDetail(LawChangeMeta(name, category, '시행', date(2026, 10, 1), date(2026, 10, 1)),
                           main_change_sections=[body] if body else [])


@pytest.mark.parametrize('name', ['보험업법', '금융투자업규정', '국민연금법', '금융회사지배구조감독규정'])
def test_title_alone_never_establishes_priority(name):
    assert assess(item('관계 기관의 협의회 구성을 정비함.', name))['status'] == 'conditional'


def test_mixed_amendment_focuses_on_employer_change():
    d = item('국민건강보험공단의 시스템 구축업무를 변경함.\n\n사용자가 통보하는 보수 총액의 통보 기한을 연장함.', '국민건강보험법 시행령')
    a = assess(d)
    assert a['status'] == 'priority' and a['rule_id'] == 'employer_payroll'
    assert '시스템' not in a['evidence'][0]['quote']


def test_fund_requires_actual_company_scope():
    a = assess(item('사내근로복지기금법인의 주택 지원 범위를 확대함.', '근로복지기본법'))
    assert a['status'] == 'conditional' and a['rule_id'] == 'welfare_fund'


def test_individual_benefit_is_reference_but_employer_change_is_not():
    assert assess(item('개인의 가입기간에 산입되는 기간을 확대함.', '국민연금법'))['status'] == 'reference'
    assert assess(item('개인의 가입기간에 산입되는 기간과 사용자의 신고사항을 변경함.', '국민연금법'))['status'] == 'conditional'


@pytest.mark.parametrize('text', ['보험회사는 대상에서 제외하며 설명 사항을 추가함.',
                                  '자동차 보험회사는 설명 사항을 추가함.'])
def test_exceptions_and_other_insurance_scope_need_review(text):
    assert assess(item(text))['status'] != 'priority'


def test_notice_and_future_date_do_not_remove_direct_relevance():
    d = item('보험회사는 설명 사항을 추가함.', category='입법예고')
    d.meta.effective_date = date(2027, 1, 1)
    assert assess(d)['status'] == 'priority'


def test_missing_or_conflicting_source_never_becomes_reference():
    d = item('보험회사는 설명 사항을 추가함.')
    d.source_limit = '서로 다른 원문 충돌'
    assert assess(d)['status'] == 'withheld'
    assert assess(item())['status'] == 'withheld'
    d = item('※ 개정이유 정보를 자동으로 추출하지 못했습니다. 원문을 직접 확인하세요.')
    assert assess(d)['status'] == 'withheld'


def test_generated_summary_cannot_decide_selection():
    d = item('위원회 내부 업무를 정비함.')
    d.summary_text = '보험회사는 설명 사항을 추가함.'
    assert assess(d)['status'] == 'conditional'


def test_review_bound_to_source_and_actual_quote():
    d = item('사내근로복지기금법인의 주택 지원 범위를 확대함.', '근로복지기본법')
    a = assess(d)
    review = {k: a[k] for k in ('candidate_id', 'source_sha256', 'evidence')}
    review.update(status='priority', reviewer='automated test fixture', reason='시험상 해당 기금 운영 사실 확인', role='기금 운영자', change='주택 지원 범위 확대')
    assert apply_review(d, review)['status'] == 'priority'
    d.main_change_sections[0] += ' 추가 예외가 있음.'
    with pytest.raises(ValueError, match='원문이 변경'): apply_review(d, review)


def test_review_cannot_release_missing_source():
    d = item(); a = assess(d)
    review = dict(a, status='priority', reviewer='test fixture', reason='시험', role='회사', change='시험')
    with pytest.raises(ValueError, match='원문 한계'): apply_review(d, review)


def test_unselected_candidates_are_recorded_and_not_enriched(tmp_path, monkeypatch):
    from law_change_auto.services import enrichment
    enrich = Mock(side_effect=AssertionError('unselected must not call AI'))
    monkeypatch.setattr(enrichment, 'enrich', enrich)
    ds = [item('개인의 가입기간에 산입되는 기간을 확대함.', '국민연금법'), item()]
    assert cli._publish_period(ds, tmp_path, date(2026,10,1), date(2026,11,30), '기간') == []
    ledger = json.loads((tmp_path/'candidate_selection.json').read_text())
    assert ledger['counts'] == {'priority':0, 'conditional':0, 'reference':1, 'withheld':1}
    assert len(json.loads((tmp_path/'collected_details.json').read_text())) == 2
    assert not enrich.called


def test_actual_cli_propagates_ai_option_and_selects_before_generation(tmp_path, monkeypatch):
    from law_change_auto.services import enrichment
    d = item('보험회사는 설명 사항을 추가함.')
    monkeypatch.setattr(cli, 'collect_details_for_range', Mock(return_value=[d]))
    notices = Mock(return_value=[])
    monkeypatch.setattr(cli, 'collect_legislation_details', notices)
    calls = Mock()  # No response or fabricated API evidence is supplied.
    def recorded(detail, *args, **kwargs):
        calls(detail.selection['status'])
        detail.impact_evidence = {'status':'failed', 'reason':'test_fixture_no_response'}
        return False
    monkeypatch.setattr(enrichment, 'enrich', recorded)
    cli.main(['--date-from','2026-10-01','--date-to','2026-11-30','--output-dir',str(tmp_path/'enabled')])
    assert calls.call_args.args == ('priority',)
    assert notices.call_args.kwargs['extract_comparisons'] is True
    calls.reset_mock()
    cli.main(['--date-from','2026-10-01','--date-to','2026-11-30','--no-summary-llm','--output-dir',str(tmp_path/'disabled')])
    assert not calls.called


def test_single_date_cli_uses_same_selection_policy(tmp_path, monkeypatch):
    ds = [item('보험회사는 설명 사항을 추가함.'),
          item('사내근로복지기금법인의 주택 지원 범위를 확대함.', '근로복지기본법')]
    monkeypatch.setattr(cli, 'collect_details_for_date', Mock(return_value=ds))
    monkeypatch.setattr(cli, 'collect_legislation_details', Mock(return_value=[]))
    cli.main(['--date','2026-10-01','--no-summary-llm','--output-dir',str(tmp_path)])
    report = json.loads((tmp_path/'run_report.json').read_text())
    assert report['mode'] == 'single-date'
    assert report['summary']['detail_count'] == 2
    assert report['summary']['individual_guide_count'] == 1


def test_notice_cli_uses_same_selection_policy(tmp_path, monkeypatch):
    from law_change_auto.services import pipeline as p
    d = item('보험회사는 설명 사항을 추가함.', category='입법예고')
    d.meta.change_type = '입법예고'
    monkeypatch.setattr(p, 'fetch_fsc_legislation_list', Mock(return_value=[d.meta]))
    monkeypatch.setattr(p, 'expand_fsc_combined_notice_metas', Mock(return_value=[d.meta]))
    monkeypatch.setattr(p, 'build_legislation_detail_from_moleg_for_fsc_split', Mock(return_value=d))
    cli.main(['--legislation','--no-summary-llm','--output-dir',str(tmp_path)])
    report = json.loads((tmp_path/'run_report.json').read_text())
    assert report['mode'] == 'legislation'
    assert report['summary']['individual_guide_count'] == 1
    assert report['selection_counts']['priority'] == 1


def test_selection_registry_tamper_blocks_delivery(tmp_path):
    from law_change_auto.services.delivery import validate_delivery
    cli._publish_period([item('보험회사는 설명 사항을 추가함.')], tmp_path, date(2026,10,1), date(2026,11,30), '기간', use_llm=False)
    ledger = tmp_path/'candidate_selection.json'
    payload = json.loads(ledger.read_text()); payload['candidates'] = []
    ledger.write_text(json.dumps(payload))
    assert any('선별 기록 해시' in e for e in validate_delivery(tmp_path)['errors'])


def test_digest_only_cannot_bypass_api_evidence(tmp_path):
    from law_change_auto.services.delivery import validate_delivery
    cli._publish_period([item('보험회사는 설명 사항을 추가함.')], tmp_path, date(2026,10,1), date(2026,11,30), '기간', use_llm=False, digest_only=True)
    result = validate_delivery(tmp_path)
    assert any('요약의 실제 LLM' in e for e in result['errors'])
    assert any('후보 선정' in e for e in result['errors'])


def test_candidate_omission_detected_even_after_rehash(tmp_path):
    from law_change_auto.services.delivery import validate_delivery, file_hash
    cli._publish_period([item('보험회사는 설명 사항을 추가함.'), item('개인의 가입기간에 산입되는 기간을 확대함.', '국민연금법')], tmp_path, date(2026,10,1), date(2026,11,30), '기간', use_llm=False)
    path = tmp_path/'candidate_selection.json'
    data = json.loads(path.read_text()); data['candidates'].pop(); path.write_text(json.dumps(data))
    manifest_path = tmp_path/'delivery_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['selection_files'][path.name] = file_hash(path)
    manifest_path.write_text(json.dumps(manifest))
    assert any('후보 또는 선별 기록 누락' in e for e in validate_delivery(tmp_path)['errors'])


def test_changed_rules_require_new_selection(tmp_path, monkeypatch):
    from law_change_auto.services import selection
    from law_change_auto.services.delivery import validate_delivery
    cli._publish_period([item('보험회사는 설명 사항을 추가함.')], tmp_path, date(2026,10,1), date(2026,11,30), '기간', use_llm=False)
    rules, _ = selection.rules_contract(); rules['version'] += '.test'
    path = tmp_path/'rules.json'; path.write_text(json.dumps(rules))
    monkeypatch.setattr(selection, 'RULES_PATH', path)
    assert any('선별 규칙 버전' in e for e in validate_delivery(tmp_path)['errors'])


def test_parser_failure_preserves_detected_candidate(monkeypatch):
    from law_change_auto.services import pipeline as p
    from law_change_auto.config.monitored_laws_loader import MonitoredLaw
    d = item('시험')
    monkeypatch.setattr(p, 'get_recent_law_changes_range', Mock(return_value=[d.meta]))
    monkeypatch.setattr(p, 'get_recent_admin_rule_changes_range', Mock(return_value=[]))
    monkeypatch.setattr(p, 'fetch_calendar_laws', Mock(return_value=[]))
    def source(match): return match, '시험 원문', None, None
    monkeypatch.setattr(p, '_fetch_detail_for_meta', source)
    monkeypatch.setattr(p, 'parse_law_change', Mock(side_effect=ValueError('test parser failure')))
    ds = p.collect_details_for_range(date(2026,10,1),date(2026,10,1),[MonitoredLaw('보험업법')],'')
    assert len(ds) == 1 and assess(ds[0])['status'] == 'withheld'
