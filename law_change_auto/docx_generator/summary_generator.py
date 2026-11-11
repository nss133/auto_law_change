"""법령제·개정 주요내용 요약 문서(docx) 생성.

기간(또는 단일 일자) 동안 수집된 LawChangeDetail 목록을 받아
법무팀 배포용 "주요내용 요약" 1건을 생성한다. 구조:

  Ⅰ. 개요          — 유형별 건수
  Ⅱ. 당사 관련성 높은 주요 개정 사항 — 생명보험사 관련 키워드 매칭 건 표
  Ⅲ. 전체 법령 제·개정 요약          — 전체 건 표
  Ⅳ. 미결사항      — 입법예고/규정변경예고·미래 시행일 등 자동 추출

각 행은 사전 생성·검토한 한 줄 요약을 사용하며, 없으면 원문 추출문을 사용한다.
렌더링 중 LLM 호출은 없으며 분류 유무와 관계없이 관련성 선별표를 유지한다.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.shared import Pt, Inches, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from ..models import LawChangeDetail

_FONT = "KoPub돋움체_Pro Light"
_FONT_BOLD = "KoPub돋움체_Pro Bold"

# 생명보험회사 업무 관련성이 높은 법령 키워드 (Ⅱ 표 선별용)
_RELEVANCE_KEYWORDS = (
    "보험", "신용정보", "자본시장", "금융투자", "금융복합", "금융지주", "금융회사", "증권",
    "개인정보", "전자금융", "여신전문", "근로자퇴직급여", "퇴직연금",
    "국민연금", "고용보험", "금융소비자", "특정금융거래", "자금세탁",
    "개인금융채권", "금융기관검사", "외부감사", "조세특례제한법", "공정거래", "근로기준", "남녀고용평등",
)


def _set_run_font(run, *, bold: bool = False, size: int = 11) -> None:
    run.font.size = Pt(size)
    name = _FONT_BOLD if bold else _FONT
    run.font.name = name
    run.bold = bold
    run.font.color.rgb = RGBColor(0, 0, 0)
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:eastAsia"), name)


def _add_para(doc, text: str = "", *, bold: bool = False, size: int = 11,
              align=None, space_after: int = 6):
    p = doc.add_paragraph()
    if text:
        _set_run_font(p.add_run(text), bold=bold, size=size)
    if align is not None:
        p.alignment = align
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.space_before = Pt(0)
    return p


def _legacy_is_relevant(detail: LawChangeDetail) -> bool:
    """Version-one delivery validation only; new runs use source-based selection."""
    name = re.sub(r"\s+", "", detail.meta.law_name or "")
    return any(re.sub(r"\s+", "", kw) in name for kw in _RELEVANCE_KEYWORDS)


def _is_relevant(detail: LawChangeDetail) -> bool:
    from ..services.selection import assess, validate_assessment
    if detail.selection:
        validate_assessment(detail)
        return detail.selection['status'] == 'priority'
    return assess(detail)['status'] == 'priority'


def _change_date_str(detail: LawChangeDetail) -> str:
    """표의 '시행/예고일' 칸 문자열."""
    meta = detail.meta
    if detail.publication_status == '별도 확인':
        d = meta.calendar_event_date
        return f'달력 {d.isoformat()} · 유형 확인 필요' if d else '날짜·유형 확인 필요'
    if meta.category == "입법예고":
        label = "예고"
        d = meta.announcement_date
        deadline = detail.opinion_deadline
        if deadline:
            return f"예고 {d.isoformat() if d else '-'} / 의견마감 {deadline}"
    else:
        label = "시행" if meta.effective_date else "공포·발령"
        d = meta.effective_date or meta.announcement_date
    if not d:
        return "-"
    return f"{label} {d.year}.{d.month}.{d.day}."


def _short_name(detail: LawChangeDetail) -> str:
    """표 '법령명' 칸: 입법예고/규정변경예고는 구분 병기."""
    # 반각 모서리 괄호는 일부 한글 글꼴/LibreOffice 조합에서 글자 폭 계산을 망가뜨린다.
    name = (detail.meta.law_name or "").strip().translate(str.maketrans({'｢': '「', '｣': '」'}))
    ct = detail.meta.change_type
    if detail.meta.category == "입법예고" and ct in ("입법예고", "규정변경예고"):
        # 이미 제목에 구분이 들어있지 않으면 병기
        if ct not in re.sub(r"\s+", "", name):
            return f"{name} ({ct})"
    return name


def _strip_lead(text: str) -> str:
    text = " ".join((text or "").split())
    text = re.sub(r'^\s*(?:(?:\d+|[가나다라마바사아])\.\s+|[·◎-]\s*)', '', text)
    # 문장 중간 머리표(가. 나. 다. / 1. 2.)는 쉼표로 치환해 한 줄로 정리
    text = re.sub(r"\s+(?:[가나다라마바사아]|\d{1,2})\.\s+", ", ", text)
    return text.strip(" ,")


def _fallback_summary(detail: LawChangeDetail) -> str:
    """LLM 미사용/실패 시 본문에서 한 줄 요약 추출."""
    pools: List[str] = []
    if detail.selection.get('status') == 'priority':
        pools.extend(e['quote'] for e in detail.selection['evidence'])
    pools.extend(detail.main_change_sections or [])
    pools.extend(detail.combined_reason_and_main_sections or [])
    pools.extend(detail.reason_sections or [])
    for raw in pools:
        s = _strip_lead(raw)
        if len(s) >= 10:
            return (s[:120].rstrip() + "…") if len(s) > 120 else s
    # 신구조문만 있는 경우
    if detail.article_comparisons:
        return f"신·구조문 대비표 {len(detail.article_comparisons)}개 항목 개정"
    return "세부 개정내용은 안내서 본문 참조"


def _one_line_summary(detail: LawChangeDetail, *, use_llm: bool) -> str:
    if detail.summary_text:
        return detail.summary_text
    # Compatibility flag only. All API calls belong to services/enrichment.py.
    return _fallback_summary(detail)


def _add_summary_table(doc, items: List[Tuple[int, LawChangeDetail, str]]) -> None:
    """[(번호, detail, 요약문), ...]로 4열 표 작성."""
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    table.autofit = False
    widths = (0.35, 2.05, 1.15, 3.65)
    for col, width in zip(table.columns, widths):
        col.width = Inches(width)
    header = OxmlElement('w:tblHeader')
    table.rows[0]._tr.get_or_add_trPr().append(header)
    headers = ("No.", "법령명", "시행/예고일", "주요 내용 요약")
    for cell, htext in zip(table.rows[0].cells, headers):
        cell.text = ""
        _set_run_font(cell.paragraphs[0].add_run(htext), bold=True, size=10)
    for no, detail, summary in items:
        row = table.add_row().cells
        for cell, val, sz in (
            (row[0], str(no), 10),
            (row[1], _short_name(detail), 10),
            (row[2], _change_date_str(detail), 10),
            (row[3], summary, 10),
        ):
            cell.text = ""
            _set_run_font(cell.paragraphs[0].add_run(val), size=sz)
    for row in table.rows:
        row._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
        for index, (cell, width) in enumerate(zip(row.cells, widths)):
            cell.width = Inches(width)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            for p in cell.paragraphs:
                p.paragraph_format.space_after = Pt(3)
                p.paragraph_format.space_before = Pt(3)
                p.paragraph_format.line_spacing = 1.1
                if index == 0:
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER


def _overview_line(details: List[LawChangeDetail]) -> str:
    law = sum(1 for d in details if d.meta.category == "법령")
    admin = sum(1 for d in details if d.meta.category == "행정규칙")
    legis = sum(1 for d in details if d.meta.category == "입법예고"
                and d.meta.change_type != "규정변경예고")
    reg = sum(1 for d in details if d.meta.change_type == "규정변경예고")
    parts = [f"법령 {law}건"]
    if admin:
        parts.append(f"행정규칙 {admin}건")
    if reg:
        parts.append(f"규정변경예고 {reg}건")
    if legis:
        parts.append(f"입법예고 {legis}건")
    return ", ".join(parts)


def _brief_name(detail: LawChangeDetail, limit: int = 22) -> str:
    """미결사항 나열용 짧은 법령명. 제목에 든 괄호를 제거하고 깔끔히 절단."""
    name = re.sub(r"[「」｢｣（）()]", "", detail.meta.law_name or "").strip()
    name = re.sub(r"\s+", " ", name)
    if len(name) > limit:
        name = name[:limit].rstrip() + "…"
    return f"「{name}」"


def _open_issues(details: List[LawChangeDetail], guide_date: date) -> List[str]:
    """미결사항 자동 추출."""
    issues: List[str] = []
    pending = [d for d in details if d.meta.category == "입법예고"
               or d.meta.change_type == "규정변경예고"]
    if pending:
        names = ", ".join(_brief_name(d) for d in pending[:4])
        more = f" 외 {len(pending) - 4}건" if len(pending) > 4 else ""
        issues.append(
            f"입법예고·규정변경예고 {len(pending)}건({names}{more})은 확정 시 내용이 "
            f"변경될 수 있으므로, 최종 확정 후 재검토가 필요함."
        )
    future = [d for d in details
              if d.meta.effective_date and d.meta.effective_date > guide_date]
    if future:
        names = ", ".join(_brief_name(d) for d in future[:4])
        more = f" 외 {len(future) - 4}건" if len(future) > 4 else ""
        issues.append(
            f"시행일 미도래 {len(future)}건({names}{more})은 시행 전 하위법령·"
            f"세부지침 동향을 지속 모니터링할 필요가 있음."
        )
    limited = [d for d in details if d.source_limit or d.impact_evidence.get('status') == 'withheld_source_limit']
    if limited:
        names = ', '.join(f'「{d.meta.law_name}」' for d in limited)
        issues.append(f'원문 미확보·자료 충돌 등 확인이 필요한 {len(limited)}건({names})은 개별 안내서에 기재된 한계를 확인하고 추가 원문 확보 후 보완할 필요가 있음.')
    relevant = [d for d in details if _is_relevant(d)]
    if relevant:
        issues.append(
            "당사 관련성 높은 개정사항은 소관 부서 검토를 거쳐 내규·약관·실무 "
            "절차에 반영할 필요가 있음."
        )
    issues.append("상세본을 생성한 경우 해당 안내서를 참조하고, 그 외 사항은 원문 및 소관 부서 검토로 확인 바람.")
    return issues


def generate_summary_docx(
    details: List[LawChangeDetail],
    output_path: Path,
    *,
    period_line: str,
    guide_date: date,
    issue_date: Optional[date] = None,
    use_llm: bool = True,
) -> Optional[Path]:
    """주요내용 요약 docx 1건 생성. 생성 실패·대상 없음 시 None.

    guide_date: '시행일 미도래' 판정 기준일. issue_date: 발행월 표기(미지정 시 guide_date).
    """
    details = [d for d in details if d.publication_status or d.has_any_content() or d.article_comparisons]
    if not details:
        return None

    # Classification must never suppress the company-relevance subset.
    summaries = [_one_line_summary(d, use_llm=False) for d in details]
    all_rows = [(i+1, _short_name(d), _change_date_str(d), summaries[i]) for i,d in enumerate(details)]
    relevant_rows = [row for row,d in zip(all_rows,details) if _is_relevant(d)]
    from .summary_reference import render_summary
    stamp = issue_date or guide_date
    overview = (f"{period_line} 기간 중 모니터링 대상 법령에서 제·개정·예고된 사항을 "
                f"유형별로 분류·요약함. 총 {len(details)}건({_overview_line(details)}).")
    issues = _open_issues(details, date.today())
    issues.insert(0, f"자료 확인 기준일은 {date.today().isoformat()}이며, 이후 새로 발표되는 자료는 포함하지 않음. 관련성 선별은 우선 확인 목록이며 실제 적용범위·조치 필요성은 원문과 업무별 요건을 확인할 사항임.")
    return render_summary(output_path, title=f"{period_line} 법령제·개정 주요내용 요약",
                          month=f"{stamp.year}. {stamp.month:02d}.", overview=overview,
                          relevant_rows=relevant_rows, all_rows=all_rows, issues=issues)
