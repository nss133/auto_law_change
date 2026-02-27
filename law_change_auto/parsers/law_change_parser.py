from __future__ import annotations

from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import html as html_module
import re

from ..models import ArticleComparisonRow, LawChangeDetail, LawChangeMeta


def _parse_revision_reason(html: str) -> list[str]:
    """제·개정이유 영역(rvsBot~rvsTop 사이) 전체 텍스트를 한 덩어리로 반환."""
    soup = BeautifulSoup(html, "lxml")

    rvs_bot = soup.find(id="rvsBot")
    rvs_top = soup.find(id="rvsTop")
    if not rvs_bot:
        return []

    between_parts: list[str] = []
    node = rvs_bot.find_next_sibling()
    while node:
        if getattr(node, "get", None) and node.get("id") == "rvsTop":
            break
        if hasattr(node, "get_text"):
            txt = node.get_text(separator=" ", strip=True)
            if txt:
                between_parts.append(txt)
        node = node.find_next_sibling()

    full_text = " ".join(between_parts).strip()
    if not full_text:
        return []

    # 공백 정리만 하고 그대로 사용
    cleaned = re.sub(r"\s+", " ", full_text).strip()
    return [cleaned] if cleaned else []


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

    revision_text_from_list가 있으면 lsRvsRsnListP.do에서 추출한 개정이유로 사용하고,
    없을 때만 revision_html을 파싱한다.
    """
    detail = LawChangeDetail(meta=meta)

    if revision_text_from_list:
        detail.combined_reason_and_main_sections.append(revision_text_from_list)
    elif revision_html:
        combined = _parse_revision_reason(revision_html)
        detail.combined_reason_and_main_sections.extend(combined)

    if old_new_html:
        detail.article_comparisons.extend(_parse_old_new_table(old_new_html))

    return detail

