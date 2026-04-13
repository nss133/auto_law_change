"""입법예고/규정변경예고 본문에서 개정이유와 주요내용을 분리하는 유틸리티.

legislation_notice_fetcher, briefing_db_fetcher 등에서 공통으로 사용한다.
"""
from __future__ import annotations

import re


_TAIL_PATTERNS = [
    re.compile(r"\n?\s*(?:\d+|다)\.\s*의견\s*제출"),
    re.compile(r"\n?\s*법령안\s*\n"),
    re.compile(r"\n?\s*규제영향분석서\s*\n"),
    re.compile(r"\n?\s*참고[·\s]*설명자료"),
]


def split_reason_and_main(text: str) -> tuple[str, str]:
    """개정이유와 주요내용을 분리한다.

    지원 패턴:
      - "1. 개정이유" / "2. 주요내용"
      - "가. 개정이유" / "나. 주요내용"
      - "개정이유 및 주요내용" (통합형) 후 "가. 개정이유" / "나. 주요내용"
      - "◎ ... 공고합니다." 형태의 공고문 헤더 제거
    """
    reason = ""
    main_content = ""

    # 공고문 헤더 제거 (◎ ~ 공고합니다 부분)
    header_end = re.search(r"공고합니다\.\s*\n", text)
    if header_end:
        text = text[header_end.end():]

    # 제목 반복 제거
    text = re.sub(r"(?:Ⅰ\.?\s*)?개정이유\s*및\s*주요내용\s*\n?", "", text, count=1).strip()

    reason_pat = re.compile(r"(?:1|가)\.\s*개정\s*이유\s*\n?")
    main_pat = re.compile(r"(?:2|나)\.\s*주요\s*내용\s*\n?")

    reason_match = reason_pat.search(text)
    main_match = main_pat.search(text)

    if reason_match and main_match and reason_match.start() < main_match.start():
        reason = text[reason_match.end():main_match.start()].strip()
        main_content = text[main_match.end():].strip()
    elif reason_match:
        reason = text[reason_match.end():].strip()
    elif main_match:
        reason = text[:main_match.start()].strip()
        main_content = text[main_match.end():].strip()
    else:
        reason = text.strip()

    # 불필요 꼬리 제거
    for ref in (reason, main_content):
        val = reason if ref is reason else main_content
        for pat in _TAIL_PATTERNS:
            m = pat.search(val)
            if m:
                val = val[:m.start()].strip()
        if ref is reason:
            reason = val
        else:
            main_content = val

    return reason, main_content
