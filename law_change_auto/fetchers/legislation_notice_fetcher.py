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
from ..parsers.content_splitter import split_reason_and_main

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
            row_start = _parse_date(cols[3].get_text(strip=True))
            row_end = _parse_date(cols[4].get_text(strip=True))

            results.append({
                "law_seq": law_seq,
                "title": title,
                "law_type_name": law_type_name,
                "ministry": ministry,
                "start_date": row_start,
                "end_date": row_end,
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
    reason, main_content = split_reason_and_main(full_text)
    result["reason"] = reason
    result["main_content"] = main_content

    return result



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


def _moleg_search_keyword_for_segment(segment_law_name: str) -> str:
    """FSC 분할 제목에서 moleg keyWord용 문자열."""
    compact = re.sub(r"\s+", "", (segment_law_name or "").strip())
    if not compact:
        return ""
    # 공통 접두가 있으면 검색 적중률 향상
    if "특정금융거래정보" in compact:
        return "특정금융거래정보"
    return compact[:14] if len(compact) >= 14 else compact


def _collect_moleg_candidates(
    keyword: str,
    fsc_announcement_date: date,
) -> List[dict]:
    """예고 시작일·기간 필터를 만족하는 검색 결과를 모은다."""
    seen: set[str] = set()
    out: List[dict] = []

    def _add_from_items(items: List[dict]) -> None:
        for item in items:
            seq = item.get("law_seq")
            if not seq or seq in seen:
                continue
            sd, ed = item.get("start_date"), item.get("end_date")
            if sd and ed and not (sd <= fsc_announcement_date <= ed):
                continue
            seen.add(seq)
            out.append(item)

    try:
        items = search_legislation_notices(
            keyword,
            max_pages=3,
            start_date=fsc_announcement_date,
            end_date=fsc_announcement_date,
        )
        _add_from_items(items)
    except Exception:
        pass

    if not out:
        try:
            items = search_legislation_notices(keyword, max_pages=3)
            _add_from_items(items)
        except Exception:
            pass

    return out


def find_moleg_meta_for_fsc_segment(
    segment_law_name: str,
    fsc_announcement_date: date | None,
    *,
    min_score: float = 0.55,
) -> LawChangeMeta | None:
    """FSC 분할 세그먼트 제목·금융위 예고일에 대응하는 moleg 입법예고 1건을 찾는다."""
    from Levenshtein import ratio as levenshtein_ratio

    from ..matching.law_matcher import _normalize_name

    if not segment_law_name or not fsc_announcement_date:
        return None

    keyword = _moleg_search_keyword_for_segment(segment_law_name)
    if len(keyword) < 4:
        return None

    candidates = _collect_moleg_candidates(keyword, fsc_announcement_date)
    if not candidates:
        return None

    norm_seg = _normalize_name(segment_law_name)
    best: dict | None = None
    best_score = 0.0

    for item in candidates:
        title = item.get("title") or ""
        if any(kw in title for kw in _ASSEMBLY_EXCLUDE_KEYWORDS):
            continue
        norm_t = _normalize_name(title)
        if not norm_t:
            continue
        s = levenshtein_ratio(norm_seg, norm_t)
        if norm_seg in norm_t or norm_t in norm_seg:
            s = max(s, 0.72)
        if s > best_score:
            best_score = s
            best = item

    if best is None or best_score < min_score:
        return None

    seq = best["law_seq"]
    return LawChangeMeta(
        law_name=best["title"],
        category="입법예고",
        change_type="입법예고",
        announcement_date=best.get("start_date"),
        effective_date=best.get("end_date"),
        source=f"moleg.go.kr:lawSeq={seq}",
        detail_url=f"{_DETAIL_URL}?lawSeq={seq}&lawType=TYPE5&mid=a10104010000",
        law_id=seq,
        law_type_label=best.get("law_type_name"),
    )


def build_legislation_detail_from_moleg_for_fsc_split(
    fsc_segment_meta: LawChangeMeta,
) -> LawChangeDetail | None:
    """FSC 통합 공지 분할 건(`law_id`에 `#s` 포함)만: moleg 상세로 안내서용 LawChangeDetail을 만든다.

    매칭 실패 시 None — 호출부에서 FSC 본문·PDF 경로로 폴백.
    """
    lid = fsc_segment_meta.law_id or ""
    if "#s" not in lid:
        return None

    moleg_meta = find_moleg_meta_for_fsc_segment(
        fsc_segment_meta.law_name,
        fsc_segment_meta.announcement_date,
    )
    if not moleg_meta:
        return None

    detail = fetch_notice_as_detail(moleg_meta)
    if not detail:
        return None

    # FSC에서 구분한 입법예고/규정변경예고·예고일(게시)은 유지, 본문·첨부는 moleg 기준
    detail.meta.change_type = fsc_segment_meta.change_type
    detail.meta.announcement_date = fsc_segment_meta.announcement_date or detail.meta.announcement_date
    detail.meta.source = f"{detail.meta.source};fsc_crosscheck={lid}"
    print(
        f"[law_change_auto] FSC 통합 공지 → moleg 상세 사용: {fsc_segment_meta.law_name[:40]}...",
        flush=True,
    )
    return detail
