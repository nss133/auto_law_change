"""국가법령정보센터 웹페이지 스크래핑 (최신법령 + 현행법령).

DRF Open API에 누락된 법령을 교차검증하기 위해,
웹사이트의 AJAX 엔드포인트(lsScListR.do)를 호출하여
공포일·시행일 기준 법령 목록을 수집한다.

검색 소스:
  1) 최신법령 > 공포법령 (tabMenuId=121) - 공포일 기준
  2) 최신법령 > 시행법령 (tabMenuId=122) - 시행일 기준
  3) 현행법령 (tabMenuId=81, 예정법령포함) - 부분 시행 예정건 포함
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Dict, List

import requests
from bs4 import BeautifulSoup

from ..models import LawChangeMeta

# 최신법령 AJAX 엔드포인트
_LIST_URL = "https://www.law.go.kr/lsScListR.do"
# tabMenuId: 121=공포법령, 122=시행법령, 81=현행법령
_TAB_PROMULGATED = "121"
_TAB_ENFORCED = "122"
_TAB_CURRENT = "81"

# 최신법령 탭 POST body (Chrome 네트워크 탭에서 캡처)
_POST_BODY_LATEST = {
    "q": "*",
    "p2": "1,2,3",              # 법령구분: 법률,대통령령,부령 등
    "p4": "110401,110402,110403,110404,110405,110406,110407",  # 소관부처
    "p19": "1,3",                # 공포구분
    "fsort": "21,11,31",         # 정렬: 공포일순
    "lsType": "7",               # 최신법령
    "section": "lawNm",
    "lsiSeq": "0",
    "p9": "1,2,4",               # 시행상태
}

# 현행법령 탭 POST body (예정법령 포함)
# - p9: "2,4" (예정법령 포함, 최신법령의 "1,2,4"와 다름)
# - p18: "0" (예정법령포함 체크)
# - lsType 없음, p2/p4 없음
_POST_BODY_CURRENT = {
    "q": "*",
    "p18": "0",                  # 예정법령포함
    "p19": "1,3",
    "fsort": "41,10,21,31",      # 시행일자순 정렬
    "lsType": "",
    "section": "lawNm",
    "lsiSeq": "0",
    "p9": "2,4",                 # 예정법령 포함 시행상태
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.law.go.kr/lsSc.do?menuId=1&subMenuId=23",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "text/html, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

# onclick="lsViewWideAll('285237','20260326', ...)"
_ONCLICK_RE = re.compile(r"lsViewWideAll\('(\d+)','(\d+)'")

# "1.  폐기물관리법 시행규칙[시행 2026. 3. 26.] [기후에너지환경부령 제33호, 2026. 3. 26., 일부개정]"
_TEXT_RE = re.compile(
    r"^\d+\.\s*(.+?)\[시행\s+([^\]]+)\]\s*\[([^\]]+)\]",
)

# "법률 제XXX호, 2026. 3. 10., 일부개정" → 공포일 + 개정구분
_BRACKET_RE = re.compile(
    r".+제\d+호,\s*(\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.?)\s*,\s*(.+)",
)


def _parse_dot_date(value: str) -> date | None:
    """'2026. 3. 10.' 또는 '2026.03.10' 형식의 날짜를 파싱."""
    if not value:
        return None
    value = value.strip().rstrip(".")
    for fmt in ("%Y. %m. %d", "%Y.%m.%d", "%Y. %m. %d."):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    # 공백이 불규칙한 경우 숫자만 추출
    nums = re.findall(r"\d+", value)
    if len(nums) == 3:
        try:
            return date(int(nums[0]), int(nums[1]), int(nums[2]))
        except ValueError:
            pass
    return None


def _fetch_page(
    pg: int,
    outmax: int = 100,
    tab_menu_id: str = _TAB_PROMULGATED,
    date_filter: str | None = None,
    max_retries: int = 3,
) -> str:
    """lsScListR.do AJAX POST 요청. 실패 시 지수 백오프로 재시도.

    Args:
        tab_menu_id: 121=공포법령, 122=시행법령, 81=현행법령
        date_filter: "YYYYMMDD~YYYYMMDD" 형식의 날짜 범위 (d1 파라미터)
    """
    if tab_menu_id == _TAB_CURRENT:
        base = _POST_BODY_CURRENT
        sub_menu_id = "15"
    else:
        base = _POST_BODY_LATEST
        sub_menu_id = "23"
    data = {**base, "pg": str(pg), "outmax": str(outmax)}
    if date_filter:
        data["d1"] = date_filter
    params = {"menuId": "1", "subMenuId": sub_menu_id, "tabMenuId": tab_menu_id}

    for attempt in range(max_retries):
        try:
            resp = requests.post(
                _LIST_URL,
                params=params,
                data=data,
                headers=_HEADERS,
                timeout=15,
            )
            resp.encoding = "utf-8"
            if resp.status_code == 200 and "error500" not in resp.text and "left_list_bx" in resp.text:
                return resp.text
        except (requests.ConnectionError, requests.Timeout):
            pass

        if attempt < max_retries - 1:
            time.sleep(2 ** (attempt + 1))  # 2s, 4s

    return ""


def _parse_law_list_html(html: str) -> List[Dict]:
    """HTML에서 법령 항목을 파싱하여 dict 리스트로 반환."""
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    ul = soup.select_one(".left_list_bx")
    if not ul:
        return []

    items: List[Dict] = []
    for li in ul.find_all("li", recursive=False):
        a = li.find("a")
        if not a:
            continue

        onclick = a.get("onclick", "")
        text = a.get_text(strip=True)

        # lsiSeq, efDate from onclick
        onclick_m = _ONCLICK_RE.search(onclick)
        if not onclick_m:
            continue
        lsi_seq = onclick_m.group(1)
        ef_date_str = onclick_m.group(2)  # YYYYMMDD

        # 법령명, 시행일, 공포정보 from text
        text_m = _TEXT_RE.match(text)
        if not text_m:
            continue

        law_name = text_m.group(1).strip()
        bracket_info = text_m.group(3).strip()  # "법률 제XXX호, 2026. 3. 10., 일부개정"

        # 공포일, 개정구분 추출
        bracket_m = _BRACKET_RE.match(bracket_info)
        anc_date_str = None
        change_type = "기타"
        if bracket_m:
            anc_date_str = bracket_m.group(1).strip()
            change_type = bracket_m.group(2).strip()

        items.append({
            "law_name": law_name,
            "lsi_seq": lsi_seq,
            "ef_date": ef_date_str,
            "anc_date_str": anc_date_str,
            "change_type": change_type,
        })

    return items


def _items_to_metas(
    items: List[Dict],
    change_label: str,
) -> List[LawChangeMeta]:
    """파싱된 항목 dict 리스트를 LawChangeMeta 리스트로 변환."""
    metas: List[LawChangeMeta] = []
    for item in items:
        anc_date = _parse_dot_date(item["anc_date_str"])
        ef_date_str = item["ef_date"]
        ef_date = None
        if ef_date_str and len(ef_date_str) == 8:
            try:
                ef_date = datetime.strptime(ef_date_str, "%Y%m%d").date()
            except ValueError:
                pass

        metas.append(
            LawChangeMeta(
                law_name=item["law_name"],
                category="법령",
                change_type=item["change_type"],
                announcement_date=anc_date,
                effective_date=ef_date,
                source=f"law.go.kr:web_scrape({change_label})",
                law_type="ls",
                lsi_seq=item["lsi_seq"],
            )
        )
    return metas


def scrape_recent_promulgated_laws(
    target_date: date,
    max_pages: int = 5,
) -> List[LawChangeMeta]:
    """웹페이지를 스크래핑하여 target_date에 공포 또는 시행되는 법령을 반환.

    3개 소스를 순차 조회하고 lsi_seq 기준으로 중복 제거:
      1) 최신법령 > 공포법령 탭(121): 공포일 기준
      2) 최신법령 > 시행법령 탭(122): 시행일 기준
      3) 현행법령 탭(81, 예정법령포함): 부분 시행 예정건 포함
    """
    ymd = target_date.strftime("%Y%m%d")
    date_filter = f"{ymd}~{ymd}"
    seen_seqs: set[str] = set()
    metas: List[LawChangeMeta] = []

    sources = [
        (_TAB_PROMULGATED, "공포"),
        (_TAB_ENFORCED, "시행"),
        (_TAB_CURRENT, "현행시행"),
    ]

    # 3개 탭의 1페이지를 병렬 요청 후, 필요 시 추가 페이지 순차 요청
    def _fetch_tab(tab_id: str, label: str) -> List[LawChangeMeta]:
        tab_metas: List[LawChangeMeta] = []
        tab_seen: set[str] = set()
        for pg in range(1, max_pages + 1):
            html = _fetch_page(pg, tab_menu_id=tab_id, date_filter=date_filter)
            items = _parse_law_list_html(html)
            if not items:
                break
            for m in _items_to_metas(items, label):
                if m.lsi_seq and m.lsi_seq not in tab_seen:
                    tab_seen.add(m.lsi_seq)
                    tab_metas.append(m)
        return tab_metas

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_fetch_tab, tab_id, label): label
                   for tab_id, label in sources}
        for future in as_completed(futures):
            for m in future.result():
                if m.lsi_seq and m.lsi_seq not in seen_seqs:
                    seen_seqs.add(m.lsi_seq)
                    metas.append(m)

    return metas


# ── calendarInfoR.do 달력 API ──────────────────────────────────────

_CALENDAR_URL = "https://law.go.kr/LSW/calendarInfoR.do"

# onclick="javascript:lsViewLsHst3('lsi_seq','공포일','공포번호','시행일','Y')"
_CALENDAR_ONCLICK_RE = re.compile(
    r"lsViewLsHst3\(\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'(\d+)'\s*,"
)

_CALENDAR_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}


def _parse_calendar_row(
    tr: "BeautifulSoup",
    fsort_label: str,
) -> LawChangeMeta | None:
    """calendarInfoR.do 테이블의 <tr> 한 행을 LawChangeMeta로 파싱.

    파싱 불가 시 None을 반환한다.
    """
    tds = tr.find_all("td")
    if len(tds) < 5:
        return None

    # 컬럼: 법령명 | 구분 | 법령종류 | 공포번호 | 소관부처
    law_name = tds[0].get_text(strip=True)
    change_type = tds[1].get_text(strip=True) or fsort_label
    act_type_name = tds[2].get_text(strip=True)
    promulgation_no_text = tds[3].get_text(strip=True)  # "제21533호" 형태
    # 소관부처는 수집하지 않음

    if not law_name:
        return None

    # onclick에서 lsi_seq, 공포일, 공포번호, 시행일 추출
    a_tag = tds[0].find("a")
    onclick = a_tag.get("onclick", "") if a_tag else ""
    onclick_m = _CALENDAR_ONCLICK_RE.search(onclick)

    lsi_seq: str | None = None
    announcement_date: date | None = None
    effective_date: date | None = None
    law_number: str | None = None

    if onclick_m:
        lsi_seq = onclick_m.group(1)
        anc_str = onclick_m.group(2)  # YYYYMMDD
        num_str = onclick_m.group(3)  # 공포번호 숫자 (leading zero 포함 가능)
        eff_str = onclick_m.group(4)  # YYYYMMDD

        law_number = str(int(num_str))  # leading zero 제거

        try:
            announcement_date = datetime.strptime(anc_str, "%Y%m%d").date()
        except ValueError:
            pass
        try:
            effective_date = datetime.strptime(eff_str, "%Y%m%d").date()
        except ValueError:
            pass

    return LawChangeMeta(
        law_name=law_name,
        category="법령",
        change_type=change_type or "기타",
        announcement_date=announcement_date,
        effective_date=effective_date,
        source="law.go.kr:calendarInfoR",
        law_type="ls",
        lsi_seq=lsi_seq,
        law_number=law_number,
        act_type_name=act_type_name or None,
        promulgation_no=law_number,
    )


def fetch_calendar_laws(
    target_date: date,
    fsort_codes: list[str] | None = None,
) -> List[LawChangeMeta]:
    """law.go.kr 달력 API(calendarInfoR.do)에서 법령 목록을 수집한다.

    Args:
        target_date: 조회 대상 날짜.
        fsort_codes: fsort 코드 리스트. 기본값 ['100', '200'] (시행+공포).
            100=시행법령, 200=공포법령, 300=폐지.

    Returns:
        LawChangeMeta 리스트 (lsi_seq 기준 중복 제거 완료).
    """
    if fsort_codes is None:
        fsort_codes = ["100", "200"]

    fsort_labels = {"100": "시행", "200": "공포", "300": "폐지"}
    cal_dt = target_date.strftime("%Y%m%d")
    seen_seqs: set[str] = set()
    metas: List[LawChangeMeta] = []

    for fsort in fsort_codes:
        label = fsort_labels.get(fsort, "기타")
        try:
            resp = requests.get(
                _CALENDAR_URL,
                params={"calDt": cal_dt, "fsort": fsort},
                headers=_CALENDAR_HEADERS,
                timeout=15,
            )
            resp.encoding = "utf-8"
            if resp.status_code != 200:
                print(
                    f"[law_change_auto] calendarInfoR 응답 오류: "
                    f"fsort={fsort}, status={resp.status_code}"
                )
                continue
        except (requests.ConnectionError, requests.Timeout) as e:
            print(f"[law_change_auto] calendarInfoR 요청 실패: fsort={fsort}, {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.find_all("tr")

        for tr in rows:
            try:
                meta = _parse_calendar_row(tr, label)
            except Exception as e:
                print(f"[law_change_auto] calendarInfoR 행 파싱 실패 (skip): {e}")
                continue
            if meta is None:
                continue
            # lsi_seq 기준 중복 제거
            if meta.lsi_seq:
                if meta.lsi_seq in seen_seqs:
                    continue
                seen_seqs.add(meta.lsi_seq)
            metas.append(meta)

    return metas


def _normalize_for_dedup(name: str) -> str:
    """중복 제거용 법령명 정규화."""
    if not name:
        return ""
    name = re.sub(r"\(.*?\)", "", name)
    name = re.sub(r"[\s·ㆍ\-_/]", "", name)
    return name.lower()


def cross_check_and_merge(
    api_metas: List[LawChangeMeta],
    web_metas: List[LawChangeMeta],
) -> List[LawChangeMeta]:
    """API 결과에 없는 웹 스크래핑 결과만 반환 (누락건)."""
    api_seqs = {m.lsi_seq for m in api_metas if m.lsi_seq}
    api_names = {_normalize_for_dedup(m.law_name) for m in api_metas}

    missing: List[LawChangeMeta] = []
    for w in web_metas:
        if w.lsi_seq and w.lsi_seq in api_seqs:
            continue
        if _normalize_for_dedup(w.law_name) in api_names:
            continue
        missing.append(w)

    return missing
