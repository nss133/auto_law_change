"""Keep the August/September summary contract even for classified period runs."""
from datetime import date
from zipfile import ZipFile

from docx import Document
import pytest

from law_change_auto.models import LawChangeDetail,LawChangeMeta
from law_change_auto.docx_generator.summary_generator import generate_summary_docx


def details():
    result=[]
    for name,status in [('보험업법','시행'),('자본시장법','향후 시행'),('금융투자업규정','예고'),('소방기본법','시행')]:
        result.append(LawChangeDetail(LawChangeMeta(name,'입법예고' if status=='예고' else '법령',
                     '입법예고' if status=='예고' else '시행',date(2026,10,1),date(2027,1,1) if status=='향후 시행' else date(2026,10,1)),
                     main_change_sections=['소방청 내부 조직을 정비함.' if name == '소방기본법' else '보험회사는 설명 사항을 추가함.'],publication_status=status))
    return result


def test_classification_never_removes_relevant_items_table(tmp_path):
    p=tmp_path/'summary.docx';generate_summary_docx(details(),p,period_line='기간',guide_date=date(2026,11,30))
    doc=Document(p)
    assert len(doc.tables)==2
    assert [r.cells[0].text for r in doc.tables[0].rows[1:]]==['1','2','3']
    assert len(doc.tables[1].rows)==5
    assert [r.cells[0].text for r in doc.tables[1].rows[1:]]==['1','2','3','4']


def test_summary_preserves_reference_parts_and_page_geometry(tmp_path):
    from law_change_auto.docx_generator.summary_reference import summary_reference_path,validate_summary
    p=tmp_path/'summary.docx';generate_summary_docx(details(),p,period_line='기간',guide_date=date(2026,11,30))
    validate_summary(p)
    with ZipFile(summary_reference_path()) as ref,ZipFile(p) as out:
        assert all(ref.read(n)==out.read(n) for n in ref.namelist() if n!='word/document.xml')
    d=Document(p)
    assert d.sections[0].left_margin.mm==31.75
    assert d.paragraphs[0].runs[0].font.name=='KoPub돋움체_Pro Bold'


def test_selected_rows_are_identical_to_corresponding_full_rows(tmp_path):
    p=tmp_path/'summary.docx';generate_summary_docx(details(),p,period_line='기간',guide_date=date(2026,11,30))
    doc=Document(p);full={r.cells[0].text:[c.text for c in r.cells]for r in doc.tables[-1].rows[1:]}
    assert all([c.text for c in r.cells]==full[r.cells[0].text] for r in doc.tables[0].rows[1:])


def test_relevance_uses_direct_source_even_when_title_has_spaces(tmp_path):
    from law_change_auto.docx_generator.summary_generator import _is_relevant
    d=details()[0];d.meta.law_name='특정 금융거래정보의 보고 및 이용 등에 관한 법률'
    assert _is_relevant(d)


def test_missing_subset_is_rejected_even_if_all_items_are_in_full_table(tmp_path):
    from lxml import etree as E
    from law_change_auto.docx_generator.summary_reference import validate_summary
    p=tmp_path/'summary.docx';generate_summary_docx(details(),p,period_line='기간',guide_date=date(2026,11,30))
    with ZipFile(p) as z:parts={n:z.read(n) for n in z.namelist()}
    ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    tree=E.fromstring(parts['word/document.xml']);body=tree.find('w:body',ns);body.remove(body.find('w:tbl',ns))
    from zipfile import ZIP_DEFLATED
    with ZipFile(p,'w',ZIP_DEFLATED) as z:
        for n,data in parts.items():z.writestr(n,E.tostring(tree) if n=='word/document.xml' else data)
    expected=[['1','보험업법','date','summary']]
    with pytest.raises(ValueError):validate_summary(p,expected_relevant=expected)


def test_source_limits_are_retained_in_open_issues(tmp_path):
    ds=details();ds[0].source_limit='개정 원문 미확보'
    p=tmp_path/'summary.docx';generate_summary_docx(ds,p,period_line='기간',guide_date=date(2026,11,30))
    text='\n'.join(p.text for p in Document(p).paragraphs)
    assert '원문 미확보·자료 충돌' in text and '「보험업법」' in text


def test_notice_label_with_spaces_is_not_duplicated():
    from law_change_auto.docx_generator.summary_generator import _short_name
    d=details()[2];d.meta.change_type='규정변경예고';d.meta.law_name='「신용정보업감독규정」 일부개정안 규정변경 예고'
    assert _short_name(d)==d.meta.law_name
