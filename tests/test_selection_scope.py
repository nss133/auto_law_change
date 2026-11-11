"""Near-miss applicability and lost deletion evidence, using synthetic sources."""
from datetime import date

import pytest

from law_change_auto.models import ArticleComparisonRow, LawChangeDetail, LawChangeMeta
from law_change_auto.parsers.law_change_parser import _parse_old_new_table
from law_change_auto.services.selection import assess, apply_review


def candidate(body, name='보험업법', old=None, new=None):
    d = LawChangeDetail(LawChangeMeta(name, '법령', '시행', date(2026, 10, 1), date(2026, 10, 1)),
                        main_change_sections=[body])
    if old is not None or new is not None:
        d.article_comparisons = [ArticleComparisonRow(article_no=None, article_title=None,
                                                      old_text=old, new_text=new)]
    return d


@pytest.mark.parametrize('markup', ['&lt;P&gt;&lt;삭  제&gt;&lt;/P&gt;',
                                    '&lt;del&gt;&lt;삭  제&gt;&lt;/del&gt;'])
def test_legal_deletion_marker_survives_html_cleanup(markup):
    xml = ('<대비표><구조문목록><조문>제1조 보험회사는 자료를 제출한다.</조문></구조문목록>'
           f'<신조문목록><조문>{markup}</조문></신조문목록></대비표>')
    row = _parse_old_new_table(xml)[0]
    assert row.new_text == '<삭  제>'


def test_missing_xml_counterpart_remains_missing():
    xml = ('<대비표><구조문목록><조문>제1조 본문</조문><조문>제2조 본문</조문></구조문목록>'
           '<신조문목록><조문>제1조 개정 본문</조문></신조문목록></대비표>')
    assert _parse_old_new_table(xml)[1].new_text is None


@pytest.mark.parametrize('body', [
    '보험회사는 설명 사항을 변경하지 않음.',
    '보험회사는 보고 대상이며 금융위원회는 설명 사항을 추가함.',
    '우선지원대상기업인 보험회사는 보고 사항을 추가함.',
    '우선지원 대상기업인 보험회사는 보고 사항을 추가함.',
])
def test_negation_other_actor_and_restricted_eligibility_are_not_priority(body):
    assert assess(candidate(body))['status'] == 'conditional'


def test_unrelated_exception_does_not_hide_a_separate_life_insurer_change():
    d = candidate('자동차보험은 대상에서 제외함. 생명보험회사는 설명 사항을 추가함.')
    result = assess(d)
    assert result['status'] == 'priority'
    assert result['evidence'][0]['quote'] == '생명보험회사는 설명 사항을 추가함.'


CHILD_SUPPORT = '양육비 이행확보 및 지원에 관한 법률'
DISCLOSURE = ('제21조의12(선지급 신청인 등의 금융정보등의 제공) 이행관리원의 장은 '
              '금융기관등의 장에게 금융정보등을 제공하도록 요청할 수 있다. 제17조제3항을 준용한다.')


def test_explicit_deleted_financial_disclosure_is_priority_with_both_sides():
    d = candidate('양육비 선지급 소득요건을 폐지함.', CHILD_SUPPORT, DISCLOSURE, '<삭  제>')
    a = assess(d)
    assert a['status'] == 'priority'
    assert a['rule_id'] == 'child_support_disclosure_repeal'
    assert [e['path'] for e in a['evidence']] == ['article_comparisons[0].old_text',
                                               'article_comparisons[0].new_text']
    review = dict(a, reviewer='synthetic test', reason='시험상 확인', role='금융기관', change='제공 근거 삭제')
    assert apply_review(d, review)['status'] == 'priority'


@pytest.mark.parametrize('new', [None, '', '(현행과 같음)', DISCLOSURE])
def test_missing_or_unchanged_counterpart_is_not_proof_of_repeal(new):
    d = candidate('양육비 선지급 소득요건을 폐지함.', CHILD_SUPPORT, DISCLOSURE, new)
    assert assess(d)['status'] == 'conditional'


def test_omnibus_and_source_limits_still_win_over_a_deletion():
    d = candidate('양육비 소득요건 폐지.', CHILD_SUPPORT, DISCLOSURE, '<삭제>')
    d.source_limit = '다른 버전과 원문 충돌'
    assert assess(d)['status'] == 'withheld'


@pytest.mark.parametrize('name,body,old,new', [
    ('소득세법 시행령', '1세대 2주택자의 양도소득세 비과세 요건을 변경함.',
     '제155조 1세대 2주택의 처분기한은 3년이다.', '제155조 1세대 2주택의 처분기한은 2년이다.'),
    ('지방세법 시행령', '1세대 일시적 2주택의 취득세 중과 요건을 변경함.',
     '제28조의5 1세대의 주택 처분기한은 3년이다.', '제28조의5 1세대의 주택 처분기한은 2년이다.'),
    (CHILD_SUPPORT+' 시행규칙', '양육비 선지급 신청서에 첨부하는 개인 증명서류를 변경함.',
     '제9조의6 양육비 채권자가 제출하는 서류는 통장 사본이다.', '제9조의6 양육비 채권자는 가족관계증명서를 제출한다.'),
    ('소방기본법', '산불 예방ㆍ진압 지원활동을 소방활동으로 변경함.',
     '제16조의2 소방서장은 산불 지원활동을 한다.', '제16조의2 소방서장은 소방활동을 한다.'),
])
def test_bounded_personal_and_public_agency_changes_are_reference(name,body,old,new):
    assert assess(candidate(body,name,old,new))['status'] == 'reference'


def test_household_tax_title_never_excludes_an_unmapped_article_or_employer_role():
    body = '1세대 주택의 양도소득세 비과세 요건을 변경함.'
    assert assess(candidate(body,'소득세법 시행령'))['status'] == 'conditional'
    d = candidate(body,'소득세법 시행령','제1조 사용자 신고기한 3일','제1조 사용자 신고기한 2일')
    assert assess(d)['status'] == 'conditional'
    d = candidate(body,'소득세법 시행령','제155조 법인의 제출기한은 3일','제155조 법인의 제출기한은 2일')
    assert assess(d)['status'] == 'conditional'


def test_removed_company_duty_blocks_personal_benefit_exclusion():
    d = candidate('개인의 가입기간에 산입되는 기간을 확대함.', '국민연금법',
                  '제1조 사용자는 보수총액을 신고한다.', '<삭제>')
    assert assess(d)['status'] != 'reference'


def test_small_business_benefit_does_not_generalize_to_every_employer():
    d = candidate('난임치료휴가 급여 지급기간을 2일에서 4일로 확대함.', '고용보험법',
                  '제75조 우선지원대상기업 근로자에게 2일의 급여를 지급한다.',
                  '제75조 우선지원대상기업 근로자에게 4일의 급여를 지급한다.')
    a = assess(d)
    assert a['status'] == 'conditional'
    assert '우선지원대상기업' in a['reason']


@pytest.mark.parametrize('field,value', [('법령일련번호','other-version'), ('시행일자','20271001')])
def test_audit_refuses_comparison_from_a_different_version(tmp_path, field, value):
    from scripts.audit_selection import restore_comparison
    d = candidate('양육비 소득요건 폐지.', CHILD_SUPPORT)
    d.meta.lsi_seq = '123'
    d.meta.promulgation_no = '456'
    fields = {'법령일련번호':'123', '법령명':CHILD_SUPPORT, '공포일자':'20261001',
              '시행일자':'20261001', '공포번호':'456'}
    fields[field] = value
    header = ''.join(f'<{k}>{v}</{k}>' for k,v in fields.items())
    path = tmp_path/'comparison.xml'
    path.write_text('<r><신조문_기본정보>'+header+'</신조문_기본정보></r>', encoding='utf-8')
    with pytest.raises(ValueError, match='version mismatch'):
        restore_comparison(d, path)
