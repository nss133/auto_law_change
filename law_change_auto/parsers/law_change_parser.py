from __future__ import annotations

from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import html as html_module
import re

from ..models import ArticleComparisonRow, LawChangeDetail, LawChangeMeta


# 개정이유 본문 앞의 불필요한 접두어 패턴
_PREFIX_RE = re.compile(
    r"^\s*(?:【제정·개정이유】|【제정ㆍ개정이유】)\s*"
)
_AMEND_TYPE_RE = re.compile(
    r"^\s*\[(?:일부개정|전부개정|타법개정|제정)\]\s*"
)


def _extract_between_rvs(html: str) -> str:
    """rvsBot~rvsTop 사이 텍스트를 개행 구분으로 추출."""
    soup = BeautifulSoup(html, "lxml")
    rvs_bot = soup.find(id="rvsBot")
    if not rvs_bot:
        return ""

    parts: list[str] = []
    node = rvs_bot.find_next_sibling()
    while node:
        if getattr(node, "get", None) and node.get("id") == "rvsTop":
            break
        if hasattr(node, "get_text"):
            txt = node.get_text(separator="\n", strip=True)
            if txt:
                parts.append(txt)
        node = node.find_next_sibling()

    return "\n".join(parts).strip()


def _split_reason_and_main(raw_text: str) -> tuple[str | None, str | None, bool]:
    """개정이유 원문에서 '개정이유'와 '주요내용'을 분리한다.

    Returns:
        (reason_text, main_text, is_combined)
        - is_combined=True: '개정이유 및 주요내용' 통합형 → reason_text에 전체, main_text=None
        - is_combined=False: 분리형 → reason_text, main_text 각각
    """
    text = raw_text.strip()
    # 접두어 제거
    text = _PREFIX_RE.sub("", text).strip()
    text = _AMEND_TYPE_RE.sub("", text).strip()

    # 통합형: "◇ 개정이유 및 주요내용"
    combined_pat = re.compile(r"◇\s*개정이유\s*및\s*주요내용\s*")
    if combined_pat.search(text):
        content = combined_pat.sub("", text).strip()
        return content, None, True

    # 분리형: "◇ 개정이유" ... "◇ 주요내용"
    reason_pat = re.compile(r"◇\s*개정이유\s*")
    main_pat = re.compile(r"◇\s*주요내용\s*")

    reason_match = reason_pat.search(text)
    main_match = main_pat.search(text)

    if reason_match and main_match:
        reason_start = reason_match.end()
        main_start = main_match.start()
        main_content_start = main_match.end()
        reason_text = text[reason_start:main_start].strip()
        main_text = text[main_content_start:].strip()
        return reason_text, main_text, False

    # ◇ 마커 없이 내용만 있는 경우 → 통합형으로 처리
    if text:
        return text, None, True

    return None, None, True


def _parse_revision_from_html(html: str) -> tuple[str | None, str | None, bool]:
    """lsRvsDocInfoR HTML에서 개정이유/주요내용을 파싱."""
    raw = _extract_between_rvs(html)
    if not raw:
        # 행정규칙: rvsBot 없음 → div.pgroup 에서 개정문 추출
        raw = _extract_admrul_revision(html)
    if not raw:
        return None, None, True
    return _split_reason_and_main(raw)


def _extract_admrul_revision(html: str) -> str:
    """행정규칙 개정문(div.pgroup)에서 텍스트 추출."""
    soup = BeautifulSoup(html, "lxml")
    pgroup = soup.find("div", class_="pgroup")
    if not pgroup:
        return ""
    return pgroup.get_text(separator="\n", strip=True)


def _parse_revision_from_text(text: str) -> tuple[str | None, str | None, bool]:
    """lsRvsRsnListP에서 가져온 텍스트를 파싱."""
    if not text or not text.strip():
        return None, None, True
    return _split_reason_and_main(text)


def _clean_markup(text: str) -> str:
    """XML 내에 섞여 있는 HTML 태그(<p>, <br> 등)를 정리한다."""
    if not text:
        return ""
    # HTML 엔티티 해제
    text = html_module.unescape(text)
    # 단순한 p/br 태그를 개행으로
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p\s*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # 남은 태그 제거
    text = re.sub(r"<[^>]+>", "", text)
    # 연속 개행 정리
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_old_new_table(xml_str: str) -> list[ArticleComparisonRow]:
    """신구법비교 XML에서 신·구 구조문 대비표를 파싱."""
    rows: list[ArticleComparisonRow] = []

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return rows

    def find_first(root_el: ET.Element, suffix: str) -> ET.Element | None:
        for el in root_el.iter():
            if el.tag.endswith(suffix):
                return el
        return None

    def find_all(root_el: ET.Element, suffix: str) -> list[ET.Element]:
        return [el for el in root_el.iter() if el.tag.endswith(suffix)]

    old_container = find_first(root, "구조문목록")
    new_container = find_first(root, "신조문목록")
    if not (old_container and new_container):
        return rows

    old_items = find_all(old_container, "조문")
    new_items = find_all(new_container, "조문")

    max_len = max(len(old_items), len(new_items))
    for i in range(max_len):
        old_el = old_items[i] if i < len(old_items) else None
        new_el = new_items[i] if i < len(new_items) else None

        def text_from(el: ET.Element | None) -> str:
            if el is None:
                return ""
            return "".join(el.itertext()).strip()

        old_text = _clean_markup(text_from(old_el))
        new_text = _clean_markup(text_from(new_el))
        if not (old_text or new_text):
            continue

        rows.append(
            ArticleComparisonRow(
                article_no=None,
                article_title=None,
                old_text=old_text or None,
                new_text=new_text or None,
            )
        )

    return rows


def parse_law_change(
    meta: LawChangeMeta,
    revision_html: str | None,
    old_new_html: str | None,
    revision_text_from_list: str | None = None,
) -> LawChangeDetail:
    """제·개정이유/신·구조문 대비표 HTML을 각각 받아 LawChangeDetail로 변환.

    개정이유/주요내용 분리 로직:
      - '◇ 개정이유 및 주요내용' → combined_reason_and_main_sections (통합형)
      - '◇ 개정이유' + '◇ 주요내용' → reason_sections + main_change_sections (분리형)
    """
    detail = LawChangeDetail(meta=meta)

    reason_text: str | None = None
    main_text: str | None = None
    is_combined = True

    if revision_text_from_list:
        reason_text, main_text, is_combined = _parse_revision_from_text(revision_text_from_list)
    elif revision_html:
        reason_text, main_text, is_combined = _parse_revision_from_html(revision_html)

    if is_combined:
        if reason_text:
            detail.combined_reason_and_main_sections.append(reason_text)
    else:
        if reason_text:
            detail.reason_sections.append(reason_text)
        if main_text:
            detail.main_change_sections.append(main_text)

    if old_new_html:
        detail.article_comparisons.extend(_parse_old_new_table(old_new_html))

    return detail
