"""briefing 시스템 SQLite DB에서 입법예고/규정변경예고 항목을 읽고,
부처 상세 페이지에서 개정이유/주요내용을 파싱하여 LawChangeDetail로 반환한다.

briefing DB는 읽기 전용으로만 사용하며, 수정하지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import List

import requests
from bs4 import BeautifulSoup

from ..models import LawChangeMeta, LawChangeDetail

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

# ── briefing DB 경로 ──────────────────────────────────────────────

_DEFAULT_DB_PATHS = [
    Path.home() / "news-clipping" / "data" / "briefing.sqlite3",
    Path.home()
    / "Library"
    / "Mobile Documents"
    / "com~apple~CloudDocs"
    / "cursor"
    / "news clipping"
    / "data"
    / "briefing.sqlite3",
]


def _find_db_path() -> Path | None:
    env = os.getenv("BRIEFING_DB_PATH")
    if env:
        p = Path(env)
        if p.exists():
            return p
    for p in _DEFAULT_DB_PATHS:
        if p.exists():
            return p
    return None


# ── DB 조회 ───────────────────────────────────────────────────────


# 허용된 부처 소스 (금융위원회, 금융감독원, 개인정보보호위원회, 공정거래위원회)
_ALLOWED_SOURCES = {"fsc", "fss", "pipc", "kftc"}

# 국회 법안 단계 항목 제외 키워드
_ASSEMBLY_EXCLUDE_KEYWORDS = ["(대안)", "위원장", "원안가결", "법제사법위원회", "수정가결"]


def _query_legislation_items(db_path: Path) -> list[dict]:
    """briefing DB에서 허용된 부처의 legislation 카테고리 항목을 읽는다."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" * len(_ALLOWED_SOURCES))
        rows = conn.execute(
            f"""
            SELECT source, category, title, url, published_at, attachments_json
            FROM items
            WHERE category IN ('legislation', 'admin_notice')
              AND source IN ({placeholders})
            ORDER BY published_at DESC
            """,
            tuple(_ALLOWED_SOURCES),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── 모니터링 법령명 매칭 ──────────────────────────────────────────


def _normalize(s: str) -> str:
    """공백·특수따옴표 제거한 정규화 문자열."""
    return re.sub(r"[\s｢｣「」\u200b]", "", s)


def match_briefing_items(
    items: list[dict],
    monitored_names: list[str],
) -> list[dict]:
    """briefing DB 항목 중 모니터링 대상 법령명이 제목에 포함된 것만 필터링.

    국회 법안 단계 항목(대안, 위원장 제출 등)은 제외한다.
    """
    norm_names = [_normalize(n) for n in monitored_names]
    matched: list[dict] = []
    for item in items:
        title = item["title"]
        # 국회 법안 단계 항목 제외
        if any(kw in title for kw in _ASSEMBLY_EXCLUDE_KEYWORDS):
            continue
        norm_title = _normalize(title)
        for nn in norm_names:
            # 법령명 앞 8자까지만 매칭 (시행령/규칙 등 하위법 포함)
            keyword = nn[:8]
            if keyword and keyword in norm_title:
                matched.append(item)
                break
    return matched


# ── 부처별 상세 페이지 파서 ───────────────────────────────────────


def _parse_fsc_detail(url: str) -> dict:
    """금융위원회(fsc.go.kr) 상세 페이지 파싱."""
    resp = requests.get(url, headers=_HEADERS, timeout=15)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    result: dict = {
        "reason": "",
        "main_content": "",
        "full_text": "",
        "notice_period": "",
        "attachments": [],
    }

    # 본문: div.content-body 안에서 메타 테이블 이후 텍스트
    body = soup.select_one("div.content-body")
    if not body:
        return result

    full_text = body.get_text(separator="\n", strip=True)
    result["full_text"] = full_text

    # 예고기간 추출
    period_match = re.search(
        r"예고기간\s*\n?\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", full_text
    )
    if period_match:
        result["notice_period"] = f"{period_match.group(1)} ~ {period_match.group(2)}"

    # 개정이유/주요내용 분리
    reason, main_content = _split_content(full_text)
    result["reason"] = reason
    result["main_content"] = main_content

    return result


def _parse_kftc_detail(url: str) -> dict:
    """공정거래위원회(ftc.go.kr) 상세 페이지 파싱."""
    resp = requests.get(url, headers=_HEADERS, timeout=15)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    result: dict = {
        "reason": "",
        "main_content": "",
        "full_text": "",
        "notice_period": "",
        "attachments": [],
    }

    # 본문
    body = soup.select_one("td.brd_cnt") or soup.select_one("div.brd_cnt")
    if body:
        full_text = body.get_text(separator="\n", strip=True)
        result["full_text"] = full_text
        reason, main_content = _split_content(full_text)
        result["reason"] = reason
        result["main_content"] = main_content

    return result


def _parse_kofiu_detail(url: str) -> dict:
    """금융정보분석원(kofiu.go.kr) 상세 페이지 파싱."""
    resp = requests.get(url, headers=_HEADERS, timeout=15)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    result: dict = {
        "reason": "",
        "main_content": "",
        "full_text": "",
        "notice_period": "",
        "attachments": [],
    }

    # 본문
    body = soup.select_one("div.view_cont") or soup.select_one("div.bbs_view_cont")
    if body:
        full_text = body.get_text(separator="\n", strip=True)
        result["full_text"] = full_text
        reason, main_content = _split_content(full_text)
        result["reason"] = reason
        result["main_content"] = main_content

    return result


def _parse_generic_detail(url: str) -> dict:
    """일반 부처 페이지 파싱 (fallback)."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return {"reason": "", "main_content": "", "full_text": "", "notice_period": "", "attachments": []}

    result: dict = {
        "reason": "",
        "main_content": "",
        "full_text": "",
        "notice_period": "",
        "attachments": [],
    }

    # 본문: 개정이유가 포함된 가장 안쪽 div/td 찾기
    for el in soup.find_all(["div", "td", "section"]):
        t = el.get_text(separator="\n", strip=True)
        if "개정이유" in t and 100 < len(t) < 10000:
            children_with_class = [c for c in el.children if hasattr(c, "get") and c.get("class")]
            if len(children_with_class) < 5:
                result["full_text"] = t
                reason, main_content = _split_content(t)
                result["reason"] = reason
                result["main_content"] = main_content
                break

    return result


# ── 공통 텍스트 분리 ─────────────────────────────────────────────


_TAIL_PATTERNS = [
    re.compile(r"\n?\s*(?:\d+|다)\.\s*의견\s*제출"),
    re.compile(r"\n?\s*법령안\s*\n"),
    re.compile(r"\n?\s*규제영향분석서\s*\n"),
    re.compile(r"\n?\s*참고[·\s]*설명자료"),
]


def _split_content(text: str) -> tuple[str, str]:
    """개정이유와 주요내용을 분리한다."""
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


# ── 소스별 파서 라우팅 ────────────────────────────────────────────

_SOURCE_PARSERS = {
    "fsc": _parse_fsc_detail,
    "kftc": _parse_kftc_detail,
    "kofiu": _parse_kofiu_detail,
}


def _parse_detail_for_source(source: str, url: str) -> dict:
    parser = _SOURCE_PARSERS.get(source, _parse_generic_detail)
    try:
        return parser(url)
    except Exception:
        return {"reason": "", "main_content": "", "full_text": "", "notice_period": "", "attachments": []}


# ── 날짜 파싱 ─────────────────────────────────────────────────────

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _parse_date_str(val: str | None) -> date | None:
    if not val:
        return None
    m = _DATE_RE.search(val)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


# ── 메인 API ──────────────────────────────────────────────────────


def get_briefing_notices_for_monitored(
    monitored_names: list[str],
    active_date: date | None = None,
) -> list[LawChangeMeta]:
    """briefing DB에서 모니터링 대상 법령의 입법예고/규정변경예고를 찾는다.

    Returns:
        매칭된 항목들의 LawChangeMeta 리스트
    """
    db_path = _find_db_path()
    if not db_path:
        return []

    items = _query_legislation_items(db_path)
    matched = match_briefing_items(items, monitored_names)
    metas: list[LawChangeMeta] = []

    for item in matched:
        pub_date = _parse_date_str(item.get("published_at"))

        metas.append(
            LawChangeMeta(
                law_name=item["title"],
                category="입법예고",
                change_type="입법예고",
                announcement_date=pub_date,
                effective_date=None,
                source=f"briefing_db:{item['source']}",
                detail_url=item["url"],
                law_id=None,
            )
        )

    return metas


def fetch_briefing_notice_detail(meta: LawChangeMeta) -> LawChangeDetail | None:
    """briefing DB에서 가져온 LawChangeMeta의 상세 페이지를 파싱하여 LawChangeDetail을 반환한다."""
    url = meta.detail_url
    if not url:
        return None

    # source 추출 (briefing_db:fsc → fsc)
    source = ""
    if meta.source and ":" in meta.source:
        source = meta.source.split(":", 1)[1]

    info = _parse_detail_for_source(source, url)

    meta.amendment_type = "입법예고"

    reason = info.get("reason", "")
    main_content = info.get("main_content", "")
    full_text = info.get("full_text", "")
    attachments = info.get("attachments", [])

    # 예고기간 추출하여 meta에 반영
    notice_period = info.get("notice_period", "")
    if notice_period:
        parts = notice_period.split("~")
        if len(parts) == 2:
            start = _parse_date_str(parts[0].strip())
            end = _parse_date_str(parts[1].strip())
            if start:
                meta.announcement_date = start
            if end:
                meta.effective_date = end

    if reason and main_content:
        return LawChangeDetail(
            meta=meta,
            reason_sections=[reason],
            main_change_sections=[main_content],
            attachments=attachments,
        )
    elif full_text:
        return LawChangeDetail(
            meta=meta,
            combined_reason_and_main_sections=[full_text],
            attachments=attachments,
        )
    else:
        # 내용 파싱 실패해도 메타데이터로 안내서 생성
        return LawChangeDetail(
            meta=meta,
            reason_sections=[],
            main_change_sections=[],
            attachments=attachments,
        )
