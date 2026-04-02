"""법제처(moleg.go.kr) 입법예고 목록 및 상세 스크래핑.

입법예고 목록 페이지에서 모니터링 대상 법령과 매칭되는 건을 조회하고,
상세 페이지에서 개정이유/주요내용을 추출한다.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import List
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from ..models import LawChangeMeta, LawChangeDetail

_LIST_URL = "https://www.moleg.go.kr/lawinfo/makingList.mo"
_DETAIL_URL = "https://www.moleg.go.kr/lawinfo/makingInfo.mo"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# 국회 법안 단계 항목 제외 키워드
_ASSEMBLY_EXCLUDE_KEYWORDS = ["(대안)", "위원장", "원안가결", "법제사법위원회", "수정가결"]


def _parse_date(text: str | None) -> date | None:
    if not text:
        return None
    m = _DATE_RE.search(text.strip())
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def search_legislation_notices(
    keyword: str,
    max_pages: int = 2,
    start_date: date | None = None,
    end_date: date | None = None,
) -> List[dict]:
    """법제처 입법예고 목록에서 keyword로 검색하여 결과를 반환한다.

    Args:
        start_date: 검색 시작일 (stYdFmt 필터, 예고 시작일 기준)
        end_date:   검색 종료일 (edYdFmt 필터, 예고 시작일 기준)

    Returns:
        list of dicts with keys: law_seq, title, law_type_name, ministry, start_date, end_date
    """
    results: List[dict] = []

    params: dict = {
        "mid": "a10104010000",
        "keyWord": keyword,
        "currentPage": "1",
        "pageCnt": "100",  # 한 번에 최대 조회
    }
    if start_date:
        params["stYdFmt"] = start_date.strftime("%Y-%m-%d")
    if end_date:
        params["edYdFmt"] = end_date.strftime("%Y-%m-%d")

    for page in range(1, max_pages + 1):
        params["currentPage"] = str(page)
        resp = requests.get(
            _LIST_URL,
            params=params,
            headers=_HEADERS,
            timeout=15,
        )
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table tbody tr")
        if not rows:
            break

        for row in rows:
            cols = row.select("td")
            if len(cols) < 5:
                continue

            # 법령종류
            law_type_name = cols[0].get_text(strip=True)
            # 입법예고명 + lawSeq
            link_tag = cols[1].select_one("a")
            if not link_tag:
                continue
            title = link_tag.get_text(strip=True)
            href = link_tag.get("href", "")
            qs = parse_qs(urlparse(href).query)
            law_seq = qs.get("lawSeq", [None])[0]
            if not law_seq:
                continue
            # 소관부처
            ministry = cols[2].get_text(strip=True)
            # 시작일자, 종료일자
            start_date = _parse_date(cols[3].get_text(strip=True))
            end_date = _parse_date(cols[4].get_text(strip=True))

            results.append({
                "law_seq": law_seq,
                "title": title,
                "law_type_name": law_type_name,
                "ministry": ministry,
                "start_date": start_date,
                "end_date": end_date,
            })

    return results


def fetch_legislation_notice_detail(law_seq: str) -> dict:
    """법제처 입법예고 상세 페이지에서 개정이유/주요내용을 추출한다.

    Returns:
        dict with keys: title, notice_number, law_type_name, notice_period,
                        ministry, department, reason, main_content, full_text
    """
    resp = requests.get(
        _DETAIL_URL,
        params={
            "lawSeq": law_seq,
            "lawCd": "0",
            "lawType": "TYPE5",
            "mid": "a10104010000",
        },
        headers=_HEADERS,
        timeout=15,
    )
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    result: dict = {
        "title": "",
        "notice_number": "",
        "law_type_name": "",
        "notice_period": "",
        "ministry": "",
        "department": "",
        "reason": "",
        "main_content": "",
        "full_text": "",
        "attachments": [],  # list of {"name": str, "url": str}
    }

    # 제목
    title_div = soup.select_one("div.tstyle_view div.title")
    if title_div:
        result["title"] = title_div.get_text(strip=True)

    # 메타 정보 (공고번호, 법령종류, 예고기간, 소관부처, 담당부서)
    head_items = soup.select("div.tstyle_view ul.head li")
    for li in head_items:
        label = li.select_one("strong")
        value = li.select_one("span")
        if not label or not value:
            continue
        key = label.get_text(strip=True)
        val = value.get_text(strip=True)
        if "공고번호" in key:
            result["notice_number"] = val
        elif "법령종류" in key:
            result["law_type_name"] = val
        elif "예고기간" in key:
            result["notice_period"] = val
        elif "소관부처" in key:
            result["ministry"] = val
        elif "담당부서" in key:
            result["department"] = val

    # 첨부파일 (lawmaking.go.kr/file/download/...)
    for a_tag in soup.select("a[href*='lawmaking.go.kr/file/download']"):
        fname = a_tag.get_text(strip=True)
        furl = a_tag.get("href", "")
        if fname and furl:
            result["attachments"].append({"name": fname, "url": furl})

    # 본문 (tb_contents)
    content_div = soup.select_one("div.tb_contents")
    if not content_div:
        return result

    full_text = content_div.get_text(separator="\n", strip=True)
    result["full_text"] = full_text

    # 개정이유 / 주요내용 분리
    reason, main_content = _split_notice_content(full_text)
    result["reason"] = reason
    result["main_content"] = main_content

    return result


def _split_notice_content(text: str) -> tuple[str, str]:
    """입법예고 본문에서 개정이유와 주요내용을 분리한다.

    지원 패턴:
      - "1. 개정이유" / "2. 주요내용"
      - "가. 개정이유" / "나. 주요내용"
      - "개정이유 및 주요내용" (통합형) 후 "가. 개정이유" / "나. 주요내용"
    """
    reason = ""
    main_content = ""

    # 통합 헤더 제거: "개정이유 및 주요내용"
    text = re.sub(r"(?:Ⅰ\.?\s*)?개정이유\s*및\s*주요내용\s*\n?", "", text, count=1).strip()

    # 여러 형태의 개정이유/주요내용 패턴
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

    # 불필요 후미 내용 제거 (의견제출, 첨부파일 영역 등)
    _TAIL_PATTERNS = [
        re.compile(r"\n?\s*(?:\d+|다)\.\s*의견\s*제출"),
        re.compile(r"\n?\s*법령안\s*\n"),
        re.compile(r"\n?\s*규제영향분석서\s*\n"),
        re.compile(r"\n?\s*참고[·\s]*설명자료"),
    ]
    for text_ref in ("main_content", "reason"):
        val = main_content if text_ref == "main_content" else reason
        for pat in _TAIL_PATTERNS:
            m = pat.search(val)
            if m:
                val = val[:m.start()].strip()
        if text_ref == "main_content":
            main_content = val
        else:
            reason = val

    return reason, main_content


def get_legislation_notices_for_monitored(
    law_names: List[str],
    active_date: date | None = None,
) -> List[LawChangeMeta]:
    """모니터링 대상 법령명으로 입법예고를 검색하고 LawChangeMeta 목록을 반환한다.

    Args:
        law_names: 검색할 법령명 리스트
        active_date: 지정 시, 해당 일자에 예고기간 진행 중인 건만 필터링
    """
    metas: List[LawChangeMeta] = []
    seen_seqs: set[str] = set()

    for name in law_names:
        # 짧은 키워드로 검색: 법령명 앞 8자 (시행령/규칙 포함 하위법령도 포함되도록)
        keyword = name.strip()[:8]
        if not keyword:
            continue

        try:
            items = search_legislation_notices(
                keyword, max_pages=1,
                start_date=active_date, end_date=active_date,
            )
        except Exception:
            continue

        norm_keyword = keyword.replace(" ", "")

        for item in items:
            seq = item["law_seq"]
            if seq in seen_seqs:
                continue

            # 검색 결과 타이틀에 키워드가 실제로 포함된 것만 (모니터링 대상과 무관한 법령 제외)
            if norm_keyword not in item["title"].replace(" ", ""):
                continue

            # 국회 법안 단계 항목 제외 (대안, 위원장 제출 등)
            if any(kw in item["title"] for kw in _ASSEMBLY_EXCLUDE_KEYWORDS):
                continue

            seen_seqs.add(seq)

            start = item.get("start_date")
            end = item.get("end_date")

            # active_date 필터: 예고기간 내인 건만
            if active_date and start and end:
                if not (start <= active_date <= end):
                    continue

            metas.append(
                LawChangeMeta(
                    law_name=item["title"],
                    category="입법예고",
                    change_type="입법예고",
                    announcement_date=start,
                    effective_date=end,  # 입법예고에서는 종료일을 effective_date로 활용
                    source=f"moleg.go.kr:lawSeq={seq}",
                    detail_url=f"{_DETAIL_URL}?lawSeq={seq}&lawType=TYPE5&mid=a10104010000",
                    law_id=seq,
                )
            )

    return metas


def fetch_notice_as_detail(meta: LawChangeMeta) -> LawChangeDetail | None:
    """입법예고 LawChangeMeta로부터 상세 내용을 가져와 LawChangeDetail을 생성한다.

    moleg.go.kr 상세 페이지가 실패하더라도 검색 메타데이터만으로 최소한의
    LawChangeDetail을 반환하여 안내서 누락을 방지한다.
    """
    law_seq = meta.law_id
    if not law_seq:
        return None

    info = fetch_legislation_notice_detail(law_seq)

    # 법령종류 레이블 설정
    meta.law_type_label = info.get("law_type_name") or None
    meta.amendment_type = "입법예고"

    reason = info.get("reason", "")
    main_content = info.get("main_content", "")
    full_text = info.get("full_text", "")
    attachments = info.get("attachments", [])

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
        # moleg.go.kr 상세 페이지 실패 시에도 메타데이터만으로 안내서 생성
        return LawChangeDetail(
            meta=meta,
            reason_sections=[],
            main_change_sections=[],
            attachments=attachments,
        )
