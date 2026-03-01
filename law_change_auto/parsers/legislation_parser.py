"""입법예고 PDF 텍스트 파싱. 개정이유·주요내용·신구조문 대비표 추출."""

from __future__ import annotations

import io
import re
from typing import List, Tuple

from ..models import ArticleComparisonRow

# PDF 추출 시 누락된 한글 공백 복원용 패턴 (순서 중요)
_SPACE_RESTORE = [
    (re.compile(r"등에관한"), "등에 관한"),
    (re.compile(r"에관한"), "에 관한"),
    (re.compile(r"에따라"), "에 따라"),
    (re.compile(r"하기위해"), "하기 위해"),
    (re.compile(r"을위해"), "을 위해"),
    (re.compile(r"를위해"), "를 위해"),
    (re.compile(r"통하여"), " 통하여"),
    (re.compile(r"또는"), " 또는 "),
    (re.compile(r"및"), " 및 "),
    (re.compile(r"법률시행령"), "법률 시행령"),
    (re.compile(r"법률시행규칙"), "법률 시행규칙"),
    (re.compile(r"포상금지급"), "포상금 지급"),
    (re.compile(r"과징금또는"), "과징금 또는"),
    (re.compile(r"부당이득의"), "부당이득의"),
    (re.compile(r"법제(\d+)"), r"법 제\1"),
    # 추가 패턴
    (re.compile(r"을국민에게"), "을 국민에게"),
    (re.compile(r"에게미리"), "에게 미리"),
    (re.compile(r"이에대한"), "이에 대한"),
    (re.compile(r"듣고자"), "듣고자 "),
    (re.compile(r"다음과같이"), "다음과 같이"),
    (re.compile(r"동개정안은"), "동 개정안은"),
    (re.compile(r"내부고발자에게"), "내부고발자에게 "),
    (re.compile(r"을전면"), "을 전면"),
    (re.compile(r"을신고"), "을 신고"),
    (re.compile(r"의범위에서"), "의 범위에서"),
    (re.compile(r"의지급"), "의 지급"),
    (re.compile(r"로위임"), "로 위임"),
    (re.compile(r"으로부터"), "으로부터 "),
    (re.compile(r"받은경우"), "받은 경우"),
    (re.compile(r"에도"), "에도 "),
    (re.compile(r"으로신고"), "으로 신고"),
    (re.compile(r"한것으로"), "한 것으로"),
    (re.compile(r"을지급"), "을 지급"),
    (re.compile(r"할수"), "할 수"),
    (re.compile(r"있도록"), "있도록 "),
    (re.compile(r"변경됨에따라"), "변경됨에 따라"),
    (re.compile(r"로실제"), "로 실제"),
    (re.compile(r"된후"), "된 후"),
    (re.compile(r"내포상금"), "내 포상금"),
]


def _restore_korean_spaces(text: str) -> str:
    """PDF 추출 시 누락된 한글 공백을 복원한다."""
    for pat, repl in _SPACE_RESTORE:
        text = pat.sub(repl, text)
    # 연속 공백 정리
    text = re.sub(r" +", " ", text)
    return text.strip()


# 섹션 경계 패턴 (개정이유 / 주요내용)
_REASON_HEADERS = re.compile(
    r"(?:제\s*[·ㆍ]\s*개정\s*이유|개정이유|제정\s*[·ㆍ]\s*개정\s*이유|조문별\s*제?\s*개정이유)"
)
_MAIN_HEADERS = re.compile(r"(?:주요\s*내용|주요\s*개정\s*사항|개정\s*내용|제\s*[·ㆍ]\s*개정\s*이유\s*및\s*주요\s*내용)")
_STOP_HEADERS = re.compile(r"(?:별표|부칙|시행\s*일|제\s*\d+\s*조)")


def _split_after_header(text: str, header_re: re.Pattern) -> Tuple[str, str]:
    """헤더 키워드 이후 첫 번째 매칭을 찾아 앞/뒤로 분리."""
    m = header_re.search(text)
    if m:
        idx = m.start()
        return text[:idx].strip(), text[idx:].strip()
    return "", text.strip()


def _extract_section_blocks(text: str, header_re: re.Pattern) -> List[str]:
    """헤더 이후부터 다음 헤더 또는 stop 패턴까지 텍스트 블록 추출."""
    blocks: List[str] = []
    for m in header_re.finditer(text):
        start = m.end()
        rest = text[start:]
        # 다음 헤더 또는 stop 패턴까지
        next_reason = _REASON_HEADERS.search(rest)
        next_main = _MAIN_HEADERS.search(rest)
        next_stop = _STOP_HEADERS.search(rest)
        candidates = [x.start() for x in (next_reason, next_main, next_stop) if x]
        end = min(candidates) if candidates else len(rest)
        block = rest[:end].strip()
        if block and len(block) > 30:
            blocks.append(block)
    return blocks


# 문단 구분 패턴 (1. 2. 3. 가. 나. 다. 라. 마. 등)
_PARAGRAPH_START = re.compile(
    r"^(\d+\.\s|가\.\s|나\.\s|다\.\s|라\.\s|마\.\s|바\.\s|사\.\s|아\.\s|◎)"
)


def _split_into_paragraphs(text: str) -> List[str]:
    """텍스트를 구조적 문단으로 분리. 1. 2. 가. 나. 등 기준으로 나눔."""
    text = _restore_korean_spaces(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    out: List[str] = []
    buf: List[str] = []
    for line in lines:
        if _PARAGRAPH_START.match(line):
            if buf:
                out.append(" ".join(buf))
                buf = []
            buf.append(line)
        elif len(line) >= 2:
            buf.append(line)
    if buf:
        out.append(" ".join(buf))
    return out


def _clean_paragraphs(text: str) -> List[str]:
    """연속 개행 정리 후 비어있지 않은 문단 리스트. 구조적 분리 우선 (기존 방식)."""
    paras = _split_into_paragraphs(text)
    if paras:
        return paras
    # fallback: 단순 라인별
    text = _restore_korean_spaces(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return [l.strip() for l in text.split("\n") if len(l.strip()) >= 2]


def parse_reason_main_from_notice_body(body_text: str) -> Tuple[List[str], List[str], str | None]:
    """게시글 본문(HTML에서 추출한 텍스트)에서 1.개정이유, 2.주요내용, 의견제출기한을 추출한다.

    Returns:
        (reason_paras, main_paras, opinion_deadline)
    """
    if not body_text or len(body_text.strip()) < 50:
        return [], [], None

    text = body_text.strip()
    text = re.sub(r"\n{3,}", "\n\n", text)

    idx_reason = re.search(r"1\.\s*개정\s*이유", text)
    idx_main = re.search(r"2\.\s*주요\s*내용", text)
    idx_etc = re.search(r"3\.\s*의견\s*제출", text)
    idx_end = re.search(r"4\.\s*그\s*밖의\s*사항", text)

    reason_paras: List[str] = []
    main_paras: List[str] = []
    opinion_deadline: str | None = None

    if idx_reason and idx_main:
        reason_block = text[idx_reason.end() : idx_main.start()].strip()
        reason_paras = _clean_paragraphs(reason_block)
    if idx_main and idx_etc:
        main_block = text[idx_main.end() : idx_etc.start()].strip()
        main_paras = _clean_paragraphs(main_block)
    if idx_etc:
        etc_block = text[idx_etc.end() : (idx_end.start() if idx_end else len(text))].strip()
        m = re.search(r"(\d{4})\s*[년\.\-]\s*(\d{1,2})\s*[월\.\-]\s*(\d{1,2})\s*일?", etc_block)
        if m:
            opinion_deadline = f"{m.group(1)}. {int(m.group(2))}. {int(m.group(3))}."

    return reason_paras[:50], main_paras[:30], opinion_deadline


def _skip_comparison_row(old_text: str, new_text: str) -> bool:
    """연락처·부처명·헤더 등 대비표에 불필요한 행 스킵."""
    if not (old_text or new_text):
        return True
    skip_terms = (
        "연락처", "전화", "팩스", "전자우편", "일반우편",
        "기획행정실", "금융소비자정책과", "중소금융과",
        "의안 소관 부서명", "문서보안",
        "기여도 평가점수", "항 목",  # 평가표 등 노이즈
    )
    if any(t in (old_text or "") or t in (new_text or "") for t in skip_terms):
        return True
    # 현행/개정안 헤더 행 스킵
    old_n = re.sub(r"\s", "", old_text or "")
    new_n = re.sub(r"\s", "", new_text or "")
    if old_n in ("현행", "개정전", "현행구조문") or new_n in ("개정안", "개정후", "개정안구조문"):
        return True
    # 둘 다 너무 짧으면 스킵 (헤더·노이즈)
    if len((old_text or "").strip()) < 5 and len((new_text or "").strip()) < 5:
        return True
    return False


def parse_comparison_from_pdf_bytes(
    pdf_items: List[Tuple[str, bytes]],
) -> List[ArticleComparisonRow]:
    """PDF 바이트 목록에서 신구조문 대비표(2열 표)를 추출한다.

    pdf_items: [(label, bytes), ...]  (라벨은 대비표 포함 여부 판별용)
    returns: ArticleComparisonRow 리스트
    """
    try:
        import pdfplumber
    except ImportError:
        return []

    rows: List[ArticleComparisonRow] = []
    seen: set[Tuple[str, str]] = set()  # (old[:200], new[:200]) 중복 제거

    for label, data in pdf_items:
        if not data or data[:4] != b"%PDF":
            continue
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages:
                    tables = page.extract_tables()
                    if not tables or sum(len(t) for t in tables) < 2:
                        tables = page.extract_tables(
                            table_settings={"vertical_strategy": "text", "horizontal_strategy": "text"}
                        )
                    if not tables:
                        continue
                    for table in tables:
                        if not table or len(table) < 2:
                            continue
                        for row in table:
                            cells = [str(c).strip() if c else "" for c in (row or [])]
                            if len(cells) < 2:
                                continue
                            # 셀 내 줄바꿈은 유지 (표 셀에 그대로 반영)
                            old_t = cells[0].replace("\r\n", "\n").replace("\r", "\n").strip()
                            new_t = cells[1].replace("\r\n", "\n").replace("\r", "\n").strip()
                            if not old_t and not new_t:
                                continue
                            if len(new_t) <= 2 and new_t not in ("개정", "신설", "삭제"):
                                continue
                            if _skip_comparison_row(old_t, new_t):
                                continue
                            if "시행" in old_t and "시행" in new_t and len(old_t) < 100 and len(new_t) < 100:
                                continue
                            # 신구조문 대비표에 넣을 행: 조문(제N조) 형식이 있는 행만 수록
                            has_article = re.search(r"제\s*\d+\s*조", old_t) or re.search(r"제\s*\d+\s*조", new_t)
                            if not has_article:
                                continue
                            key = (old_t[:200], new_t[:200])
                            if key in seen:
                                continue
                            seen.add(key)
                            rows.append(
                                ArticleComparisonRow(
                                    article_no=None,
                                    article_title=None,
                                    old_text=old_t or None,
                                    new_text=new_t or None,
                                    old_segments=None,
                                    new_segments=None,
                                )
                            )
        except Exception:
            continue

    return rows
