from __future__ import annotations

import difflib
import html as html_module
import re

from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET

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
            text = sibling.get_text(separator="\n", strip=True)
            if text and text not in ("【제정·개정이유】",):
                results.append(text)

    # 타법개정: 내용이 없고 '타법개정 제개정이유' 버튼만 있는 경우는 상위에서 따로 처리할 수 있도록 특수 마커 반환
    joined = "\n".join(results)
    if not joined:
        talbeop_btn = soup.find(string=re.compile(r"타법개정\s*제개정이유"))
        if talbeop_btn:
            return [_TALBEOP_MARKER]
    return results


def _parse_admrul_revision(soup: BeautifulSoup) -> list[str]:
    """행정규칙 admRulRvsInfoR.do 파싱.

    rvsConScroll 또는 contentBody 내에 【제정·개정이유】 이후 본문이 위치.
    pgroup 등 블록 구조는 get_text 후 헤더 이후 라인으로 추출.
    """
    content_body = soup.find(id="contentBody")
    if content_body:
        results = _extract_after_header(content_body, "제정·개정이유")
        if results:
            return results

    scroll_div = soup.find(id="rvsConScroll")
    if scroll_div:
        results = _extract_after_header(scroll_div, "제정·개정이유")
        if results:
            return results
        # fallback: 말단 요소별 수집
        return _extract_texts_from_container(scroll_div)

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
            text = elem.get_text(separator="\n", strip=True)
            if text and not skip_patterns.match(text):
                results.append(text)
    return results


def _extract_after_header(container: BeautifulSoup, header_keyword: str) -> list[str]:
    """헤더 키워드 이후 텍스트 수집 (패턴 기반 fallback)."""
    all_text = container.get_text(separator="\n")
    lines = [l.strip() for l in all_text.split("\n") if l.strip()]

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
            block = parent.get_text(separator="\n", strip=True)
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
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p\s*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    # 남은 태그 제거
    text = re.sub(r"<[^>]+>", "", text)
    # 연속 개행 정리
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_segments_from_element(el: ET.Element) -> list[tuple[str, str]]:
    """XML 요소에서 ins/del 마크업을 보존한 (text, style) 세그먼트 목록 추출.

    style: "normal" | "ins" (추가) | "del" (삭제)
    - XML에 ins/del 자식 요소가 있으면 구조 순회
    - 내부가 HTML 문자열(예: &lt;ins&gt;추가&lt;/ins&gt;)이면 BeautifulSoup으로 파싱
    """
    raw_html = ET.tostring(el, encoding="unicode", method="xml")
    raw_html = html_module.unescape(raw_html)

    # HTML 문자열로 ins/del이 포함된 경우 BeautifulSoup으로 파싱
    if "<ins" in raw_html or "<del" in raw_html:
        try:
            soup = BeautifulSoup(raw_html, "html.parser")
            html_segments: list[tuple[str, str]] = []

            def _collect(node, inherit: str) -> None:
                if hasattr(node, "name") and node.name:
                    style = inherit
                    if node.name == "ins":
                        style = "ins"
                    elif node.name == "del":
                        style = "del"
                    for c in node.children:
                        if isinstance(c, str):
                            t = re.sub(r"<[^>]+>", "", c).strip()
                            t = re.sub(r"<br\s*/?>", "\n", t, flags=re.IGNORECASE)
                            if t:
                                html_segments.append((t, style))
                        else:
                            _collect(c, style)
                elif isinstance(node, str):
                    t = re.sub(r"<[^>]+>", "", node).strip()
                    if t:
                        html_segments.append((t, inherit))

            for child in soup.children:
                _collect(child, "normal")
            if html_segments:
                return html_segments
        except Exception:
            pass

    # XML 구조 기반 순회 (ins/del 실제 자식 요소)
    segments = []

    def _add(t: str, style: str) -> None:
        t = html_module.unescape(t or "")
        t = re.sub(r"<br\s*/?>", "\n", t, flags=re.IGNORECASE)
        t = re.sub(r"<p\s*>", "", t, flags=re.IGNORECASE)
        t = re.sub(r"</p\s*>", "\n", t, flags=re.IGNORECASE)
        t = re.sub(r"<[^>]+>", "", t)
        t = t.strip()
        if t:
            segments.append((t, style))

    def _local_tag(e: ET.Element) -> str:
        return e.tag.split("}")[-1] if "}" in e.tag else e.tag

    def _walk(elem: ET.Element, inherit_style: str) -> None:
        if elem.text:
            _add(elem.text, inherit_style)
        for child in elem:
            tag = _local_tag(child).lower()
            style = inherit_style
            if tag == "ins":
                style = "ins"
            elif tag == "del":
                style = "del"
            _walk(child, style)
            if child.tail:
                _add(child.tail, inherit_style)

    _walk(el, "normal")
    return segments


def _segments_to_plain(segments: list[tuple[str, str]]) -> str:
    """세그먼트 목록을 평문으로 합친다."""
    return "".join(t for t, _ in segments).strip()


def _diff_to_segments(old_text: str, new_text: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """구문/신문 diff로 ins/del 세그먼트 생성. (API에 마크업 없을 때 사용)."""
    old_text = old_text or ""
    new_text = new_text or ""
    matcher = difflib.SequenceMatcher(None, old_text, new_text)
    old_segments: list[tuple[str, str]] = []
    new_segments: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            s = old_text[i1:i2]
            if s:
                old_segments.append((s, "normal"))
                new_segments.append((s, "normal"))
        elif tag == "delete":
            s = old_text[i1:i2]
            if s:
                old_segments.append((s, "del"))
        elif tag == "insert":
            s = new_text[j1:j2]
            if s:
                new_segments.append((s, "ins"))
        elif tag == "replace":
            if i1 < i2:
                old_segments.append((old_text[i1:i2], "del"))
            if j1 < j2:
                new_segments.append((new_text[j1:j2], "ins"))
    return (
        _merge_whitespace_into_del_ins(old_segments),
        _merge_whitespace_into_del_ins(new_segments),
    )


def _merge_whitespace_into_del_ins(segments: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """인접한 del/ins 사이에 공백만 있는 normal을 흡수하여 밑줄·색상 끊김 방지."""
    if not segments:
        return []
    result: list[tuple[str, str]] = []
    i = 0
    while i < len(segments):
        text, style = segments[i]
        # del/ins 뒤에 공백만 있는 normal들, 그 다음 같은 style del/ins가 있으면 모두 흡수
        while style in ("del", "ins") and i + 1 < len(segments):
            # 공백만 있는 normal 세그먼트들 수집
            j = i + 1
            whitespace = ""
            while j < len(segments):
                mid_text, mid_style = segments[j]
                if mid_style != "normal" or mid_text.strip():
                    break
                whitespace += mid_text
                j += 1
            # 그 다음 같은 style del/ins가 있어야 흡수
            if j < len(segments):
                next_text, next_style = segments[j]
                if next_style == style and whitespace:
                    text += whitespace + next_text
                    i = j
                    continue
            break
        result.append((text, style))
        i += 1
    return result


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

        def _segments_from(el: ET.Element | None) -> tuple[list[tuple[str, str]], str]:
            if el is None:
                return [], ""
            segments = _extract_segments_from_element(el)
            plain = _segments_to_plain(segments) if segments else ""
            # 마크업 없으면 평문만 있을 수 있음 → itertext 기반 fallback
            if not segments:
                raw = "".join(el.itertext()).strip()
                plain = _clean_markup(raw)
                if plain:
                    segments = [(plain, "normal")]
            return segments, plain

        old_segments, old_text = _segments_from(old_el)
        new_segments, new_text = _segments_from(new_el)
        if not (old_text or new_text):
            continue
        # API에 ins/del 마크업이 없으면 difflib으로 diff 세그먼트 생성
        has_markup = any(
            s == "ins" or s == "del"
            for segs in (old_segments, new_segments)
            for _, s in segs
        )
        if not has_markup and (old_text or new_text):
            old_segments, new_segments = _diff_to_segments(old_text, new_text)
        # 공백 끊김 보정: 인접 del/ins 사이 공백만 있는 normal 흡수
        old_segments = _merge_whitespace_into_del_ins(old_segments)
        new_segments = _merge_whitespace_into_del_ins(new_segments)
        rows.append(
            ArticleComparisonRow(
                article_no=None,
                article_title=None,
                old_text=old_text or None,
                new_text=new_text or None,
                old_segments=old_segments if old_segments else None,
                new_segments=new_segments if new_segments else None,
            )
        )
    return rows


def _split_eflaw_reason_and_main(text: str) -> tuple[list[str], list[str]]:
    """eflaw 본문을 '◇ 주요내용' 경계로 개정이유 / 주요내용으로 분리."""
    reason_blocks: list[str] = []
    main_blocks: list[str] = []
    if not text:
        return reason_blocks, main_blocks

    # "◇ 주요내용" 또는 "◇ 개정이유 및 주요내용"으로 분할
    main_header = "◇ 주요내용"
    combined_header = "◇ 개정이유 및 주요내용"
    if main_header in text:
        before, after = text.split(main_header, 1)
        reason_blocks.append(before.strip())
        main_blocks.append(after.strip())
    elif combined_header in text:
        before, after = text.split(combined_header, 1)
        reason_blocks.append(before.strip())
        main_blocks.append(after.strip())
    else:
        reason_blocks.append(text.strip())

    return reason_blocks, main_blocks


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
    reason_lines: list[str] = []
    if revision_text_from_list:
        # eflaw: ◇ 주요내용 경계로 개정이유/주요내용 분리
        reason_blocks, main_blocks = _split_eflaw_reason_and_main(revision_text_from_list)
        detail.reason_sections = [b for b in reason_blocks if b]
        detail.main_change_sections = [b for b in main_blocks if b]
        detail.combined_reason_and_main_sections = reason_blocks + main_blocks
    elif revision_html:
        source_type = "admrul" if meta.law_type == "admrul" else "ls"
        combined = _parse_revision_reason(revision_html, source_type=source_type)
        if combined == [_TALBEOP_MARKER]:
            detail.reason_sections = combined
            detail.main_change_sections = []
            detail.combined_reason_and_main_sections = combined
        else:
            # HTML: 줄 단위로 개정이유/주요내용 분리
            current_reason = []
            current_main = []
            is_main_section = False
            for line in combined:
                if "◇" in line and "주요내용" in line:
                    is_main_section = True
                if is_main_section:
                    current_main.append(line)
                else:
                    current_reason.append(line)
            detail.reason_sections = current_reason
            detail.main_change_sections = current_main
            detail.combined_reason_and_main_sections = combined

    # 3. 신구조문 대비표
    if old_new_html:
        detail.article_comparisons.extend(_parse_old_new_table(old_new_html))

    return detail
