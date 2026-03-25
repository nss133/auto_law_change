"""입법예고·규정변경예고 수집. 금융위원회(FSC) po040301."""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

from ..config.monitored_laws_loader import MonitoredLaw
from ..models import LawChangeMeta
from .pdf_extractor import download_pdf, extract_text_from_pdf_bytes


def _is_regulation_gosi_label(label: str) -> bool:
    """'규정 일부개정고시안' 등 신구조문 대비표 포함 PDF인지 판별. 공고·조문별이유서·규제영향분석서 등 제외."""
    if "규정" not in label or "고시안" not in label:
        return False
    # 제외: 공고문, 조문별 재/제개정이유서, 규제영향분석서, 미첨부 확인서 등
    exclude = ("공고", "조문별", "이유서", "규제영향", "미첨부", "확인서", "사전예고기간")
    if any(x in label for x in exclude):
        return False
    # 포함: 일부개정고시안, 개정고시안 (규정 본문·신구조문 대비표 포함)
    return "일부개정고시안" in label or "개정고시안" in label


def download_and_save_gosi_pdfs(
    detail_url: str,
    output_dir: Path,
    session: Optional[requests.Session] = None,
) -> List[tuple[str, str]]:
    """규정 일부/개정고시안 첨부 PDF를 다운로드해 output_dir에 저장하고 (라벨, 저장경로) 반환.
    공고문·조문별이유서·규제영향분석서 등은 제외."""
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        session.get(detail_url, timeout=20)
    pdfs = fetch_fsc_notice_pdf_urls(detail_url)
    result: List[tuple[str, str]] = []
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for label, url in pdfs:
        if not _is_regulation_gosi_label(label):
            continue
        data = download_pdf(url, session=session)
        if not data or data[:4] != b"%PDF":
            continue
        # 파일명: 라벨에서 .pdf 앞부분만 사용, 특수문자 제거
        name = re.sub(r"[^\w\s\-\.가-힣]", "", label)
        name = re.sub(r"\s+", "_", name).strip("_") or "규정고시안"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        path = output_dir / name
        try:
            path.write_bytes(data)
            result.append((label, str(path.resolve())))
            print(f"[law_change_auto] PDF 저장: {path}")
        except Exception as e:
            print(f"[law_change_auto] PDF 저장 실패 ({name}): {e}")
    return result

FSC_LEGISLATION_LIST = "https://www.fsc.go.kr/po040301"
FSC_BASE = "https://www.fsc.go.kr"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (law_change_auto)",
    "Referer": "https://www.fsc.go.kr/",
}


def _parse_yyyy_mm_dd(text: str) -> Optional[date]:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def _extract_notice_id(url: str) -> Optional[str]:
    qs = parse_qs(urlparse(url).query)
    notice_ids = qs.get("noticeId") or qs.get("noticeid")
    return notice_ids[0] if notice_ids else None


def _get_text(elem, strip: bool = True) -> str:
    t = (elem.get_text(" ", strip=False) if hasattr(elem, "get_text") else "") or ""
    return t.strip() if strip else t


def fetch_fsc_legislation_list(max_items: int = 50) -> List[LawChangeMeta]:
    """금융위원회 입법예고/규정변경예고 목록을 수집한다."""
    metas: List[LawChangeMeta] = []
    try:
        resp = requests.get(FSC_LEGISLATION_LIST, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception:
        return metas

    for a in soup.select('a[href*="/po040301/view?"]')[:max_items]:
        href = a.get("href") or ""
        title = _get_text(a)
        if not href or not title:
            continue
        url = href if href.startswith("http") else urljoin(FSC_BASE, href.lstrip("./"))
        notice_id = _extract_notice_id(url)
        if not notice_id:
            continue

        # 날짜·구분은 a.parent 다음 형제(div)에 있음 (예: "구분 : 입법예고 ... 예고기간 : 2026-02-26 ~ ...")
        meta_div = None
        if a.parent and hasattr(a.parent, "find_next_sibling"):
            meta_div = a.parent.find_next_sibling()
        row_text = _get_text(meta_div) if meta_div else ""
        if not row_text and a.parent:
            row_text = _get_text(a.find_parent("div", class_="cont")) if hasattr(a, "find_parent") else ""
        announcement_date = _parse_yyyy_mm_dd(row_text)

        # 입법예고/규정변경예고 구분
        change_type: str = "입법예고"
        if "규정변경예고" in row_text or "규정변경 예고" in row_text:
            change_type = "규정변경예고"

        metas.append(
            LawChangeMeta(
                law_name=title,
                category="입법예고",
                change_type=change_type,
                announcement_date=announcement_date,
                source="fsc:po040301",
                detail_url=url,
                law_id=notice_id,
                law_type=None,
            )
        )
    return metas


def fetch_notice_body_text(detail_url: str) -> str:
    """게시글 본문(HTML)에서 본문 텍스트를 추출한다. 개정이유·주요내용은 여기서 파싱."""
    try:
        resp = requests.get(detail_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception:
        return ""
    # 본문 영역: FSC·일반 게시판 클래스 시도
    for sel in (
        ".board_view", ".view_content", ".bbs_content", ".content_area",
        ".board_detail", ".detail_content", ".board_body", "#contents", "main",
    ):
        el = soup.select_one(sel)
        if el and ("개정이유" in el.get_text() or "주요내용" in el.get_text()):
            return _get_text(el, strip=True)
    # fallback: 첨부파일 링크 이전까지 body 텍스트
    body = soup.find("body")
    if body:
        text = body.get_text(separator="\n", strip=False)
        # "첨부파일" 이전까지만 (첨부 목록 제외)
        idx = text.find("첨부파일")
        if idx > 0:
            text = text[:idx]
        return text.strip()
    return ""


def fetch_fsc_notice_pdf_urls(detail_url: str) -> List[tuple[str, str]]:
    """상세 페이지에서 PDF 첨부 (라벨, URL) 목록을 추출한다.

    라벨은 부모 요소 텍스트에서 .pdf 이전 부분을 사용.
    """
    result: List[tuple[str, str]] = []
    try:
        resp = requests.get(detail_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception:
        return result

    for a in soup.select('a[href*="getFile"]'):
        href = a.get("href") or ""
        if not href:
            continue
        url = href if href.startswith("http") else urljoin(FSC_BASE, href.replace("&amp;", "&"))
        # 부모에서 파일명 추출 (예: "1. 금융위원회 공고 제2026-167호(...).pdf (183 KB)")
        parent = a.parent
        label = ""
        while parent and len(label) < 200:
            t = _get_text(parent)
            m = re.search(r"([^\[]*?\.pdf)\s*(?:\(\d+\s*[KkMmBb]\))?", t, re.IGNORECASE)
            if m:
                label = m.group(1).strip()
                break
            parent = getattr(parent, "parent", None)
        if not label:
            label = "첨부"
        if ".pdf" not in label.lower():
            continue
        result.append((label, url))
    return result


def _pdf_priority(item: tuple[str, str]) -> int:
    """공고문·조문별·대비표·개정안 우선순위."""
    label = item[0]
    if "공고" in label and "제" in label:
        return 0
    if "조문별" in label or "제개정이유" in label or "재개정이유" in label:
        return 1
    if "대비" in label or "별표" in label or "신구조문" in label:
        return 2  # 신구조문 대비표 PDF 우선
    if "개정령안" in label or "개정고시안" in label:
        return 3
    return 4


def fetch_notice_pdf_texts(
    detail_url: str,
    max_pdfs: int = 20,
    session: Optional[requests.Session] = None,
) -> List[tuple[str, str]]:
    """상세 페이지에서 PDF 첨부를 다운로드해 텍스트를 추출한다.

    Returns:
        [(filename_or_label, extracted_text), ...]
    """
    full = fetch_notice_pdf_full(detail_url, max_pdfs=max_pdfs, session=session)
    return [(label, text) for label, text, _ in full if text and len(text.strip()) > 50]


def fetch_notice_pdf_full(
    detail_url: str,
    max_pdfs: int = 20,
    session: Optional[requests.Session] = None,
) -> List[tuple[str, str, bytes]]:
    """상세 페이지에서 PDF 첨부를 다운로드해 (라벨, 텍스트, 바이트)를 반환한다.

    신구조문 대비표 추출을 위해 바이트도 함께 반환.
    """
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        session.get(detail_url, timeout=20)
    pdfs = fetch_fsc_notice_pdf_urls(detail_url)
    pdfs.sort(key=_pdf_priority)
    out: List[tuple[str, str, bytes]] = []
    for label, url in pdfs[:max_pdfs]:
        data = download_pdf(url, session=session)
        if not data or data[:4] != b"%PDF":
            continue
        text = extract_text_from_pdf_bytes(data)
        out.append((label, text, data))
    return out
