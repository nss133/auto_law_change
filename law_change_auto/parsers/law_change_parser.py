from __future__ import annotations

from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import html as html_module
import re

from ..models import ArticleComparisonRow, LawChangeDetail, LawChangeMeta

# 타법개정 특수 마커 (lsInfoP 페이지에 본문 없이 버튼만 있는 경우)
_TALBEOP_MARKER = "__TALBEOP_KAEJUNG__"


def _parse_ls_revision(soup: BeautifulSoup) -> list[str]:
    """법령 lsInfoP?viewCls=lsRvsDocInfoR 파싱.

    rvsBot/rvsTop는 위치 마커(div)이고, 그 사이 형제 노드들의 텍스트가 실제 개정이유이다.
    """
    rvs_bot = soup.find(id="rvsBot")
    rvs_top = soup.find(id="rvsTop")
    if not rvs_bot or not rvs_top:
        return []

    results: list[str] = []
    for sibling in rvs_bot.find_next_siblings():
        if getattr(sibling, "get", None) and sibling.get("id") == "rvsTop":
            break
        if hasattr(sibling, "get_text"):
            text = sibling.get_text(separator="
", strip=True)
            if text and text not in ("【제정·개정이유】",):
                results.append(text)

    # 타법개정: 내용이 없고 '타법개정 제개정이유' 버튼만 있는 경우는 상위에서 따로 처리할 수 있도록 특수 마커 반환
    joined = "
".join(results)
    if not joined:
        talbeop_btn = soup.find(string=re.compile(r"타법개정\s*제개정이유"))
        if talbeop_btn:
            return [_TALBEOP_MARKER]
    return results


def _parse_admrul_revision(soup: BeautifulSoup) -> list[str]:
    """행정규칙 admRulInfoP?urlMode=admRulRvsInfoR 파싱.

    rvsBot/rvsTop 없이 rvsConScroll 내에 제정·개정이유가 위치.
    """
    scroll_div = soup.find(id="rvsConScroll")
    if scroll_div:
        return _extract_texts_from_container(scroll_div)

    content_body = soup.find(id="contentBody")
    if content_body:
        return _extract_after_header(content_body, "제정·개정이유")

    return []


def _extract_texts_from_container(container: BeautifulSoup) -> list[str]:
    """컨테이너 내 텍스트 노드 수집 (버튼/링크 제외)."""
    results: list[str] = []
    skip_patterns = re.compile(r"^(【제정·개정문】|제정·개정이유보기|전체 제정·개정문보기)$")

    for elem in container.find_all(True):
        if elem.name in ("a", "img", "button"):
            continue
        # 자식 태그가 있는 컨테이너는 건너뛰고 말단 요소만 수집
        if elem.find(True):
            continue
        if hasattr(elem, "get_text"):
            text = elem.get_text(separator="
", strip=True)
            if text and not skip_patterns.match(text):
                results.append(text)
    return results


def _extract_after_header(container: BeautifulSoup, header_keyword: str) -> list[str]:
    """헤더 키워드 이후 텍스트 수집 (패턴 기반 fallback)."""
    all_text = container.get_text(separator="
")
    lines = [l.strip() for l in all_text.split("
") if l.strip()]

    collecting = False
    results: list[str] = []
    stop_patterns = re.compile(r"^【제정·개정문】")

    for line in lines:
        if header_keyword in line:
            collecting = True
            continue
        if collecting:
            if stop_patterns.match(line):
                break
            results.append(line)
    return results


def _extract_fallback(soup: BeautifulSoup) -> list[str]:
    """어떤 구조도 매칭 안 됐을 때 최후 수단."""
    diamond_texts: list[str] = []
    for elem in soup.find_all(string=re.compile(r"^◇")):
        parent = getattr(elem, "parent", None)
        if parent and hasattr(parent, "get_text"):
            block = parent.get_text(separator="
", strip=True)
            diamond_texts.append(block)

    if diamond_texts:
        return diamond_texts

    return ["※ 개정이유 정보를 자동으로 추출하지 못했습니다. 원문을 직접 확인하세요."]


def _parse_revision_reason(html: str, source_type: str = "ls") -> list[str]:
    """제·개정이유 텍스트를 리스트로 반환.

    source_type: "ls" (법령) | "admrul" (행정규칙)
    """
    soup = BeautifulSoup(html, "html.parser")

    if source_type == "ls":
        results = _parse_ls_revision(soup)
    elif source_type == "admrul":
        results = _parse_admrul_revision(soup)
    else:
        results = []

    # 타법개정 마커는 fallback 없이 그대로 반환 (상위에서 처리)
    if results == [_TALBEOP_MARKER]:
        return results

    if not results:
        results = _extract_fallback(soup)

    return results


def _clean_markup(text: str) -> str:
    """XML 내에 섞여 있는 HTML 태그(<p>, <br>
등)를 정리한다."""
    if not text:
        return ""
    # HTML 엔티티 해제
    text = html_module.unescape(text)
    # 단순한 p/br 태그를 개행으로
    text = re.sub(r"<br\s*/?>", "
", text, flags=re.IGNORECASE)
    text = re.sub(r"<p\s*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "
", text, flags=re.IGNORECASE)
    # 남은 태그 제거
    text = re.sub(r"<[^>]+>", "", text)
    # 연속 개행 정리
    text = re.sub(r"
{3,}", "

", text)
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

    # 1. 제·개정이유 수집
    reason_lines = []
    if revision_text_from_list:
        reason_lines.append(revision_text_from_list)
    elif revision_html:
        source_type = "admrul" if meta.law_type == "admrul" else "ls"
        combined = _parse_revision_reason(revision_html, source_type=source_type)
        if combined != [_TALBEOP_MARKER]:
            reason_lines.extend(combined)

    # 2. 개정이유와 주요내용 분리 (◇ 기호 기반)
    current_reason = []
    current_main = []
    is_main_section = False

    for line in reason_lines:
        # "주요내용" 키워드가 포함된 ◇ 제목이 나오면 그 이후는 주요내용으로 간주
        if "◇" in line and "주요내용" in line:
            is_main_section = True
        
        if is_main_section:
            current_main.append(line)
        else:
            current_reason.append(line)

    detail.reason_sections = current_reason
    detail.main_change_sections = current_main
    # 하위 호환성을 위해 combined도 유지
    detail.combined_reason_and_main_sections = reason_lines

    # 3. 신구조문 대비표
    if old_new_html:
        detail.article_comparisons.extend(_parse_old_new_table(old_new_html))

    return detail
