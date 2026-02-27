from __future__ import annotations

import re
import requests
from bs4 import BeautifulSoup

from ..models import LawChangeMeta
from .national_law_fetcher import _get_oc


HEADERS = {
    "User-Agent": "Mozilla/5.0 (law_change_auto)",
    "Referer": "https://www.law.go.kr/",
}


_META_LINE_RE = re.compile(
    r"\[시행\s+[^\]]+\]\s*\[(?:법률|대통령령|총리령|부령)\s*제(\d+)호,\s*([^,\]]+),\s*([^\]]+)\]"
)


def fetch_revision_reason_from_ls_rvs_rsn_list(
    ls_id: str,
    chr_cls_cd: str,
    target_date_str: str,
) -> tuple[str, dict | None]:
    """lsRvsRsnListP.do에서 특정 시행일 버전의 개정이유를 추출한다.

    <법제처 제공>으로 구분된 블록 중 target_date_str이 포함된 시행 블록을 찾고,
    해당 블록에서 【제정·개정이유】 이후 텍스트를 반환한다.
    블록 내에 "⊙법률 제...호" 패턴이 있으면 타법개정으로 보고 빈 문자열을 반환한다.

    Args:
        ls_id: 법령 ID (예: "001532")
        chr_cls_cd: 개정구분코드 (예: "010202")
        target_date_str: 찾을 시행일 (예: "2024. 10. 25.")

    Returns:
        (개정이유 텍스트, 메타데이터 또는 None). 메타데이터는 law_number, amendment_date_str, amendment_type 키를 가짐.
    """
    url = (
        "https://www.law.go.kr/LSW/lsRvsRsnListP.do"
        f"?lsId={ls_id}&chrClsCd={chr_cls_cd}&lsRvsGubun=all"
    )
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    full_text = resp.text

    wrap = BeautifulSoup(full_text, "html.parser").find(id="viewwrapCenter")
    if not wrap:
        return "", None

    body_text = wrap.get_text(separator="\n", strip=True)
    blocks = re.split(r"\s*<법제처 제공>\s*", body_text)

    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if target_date_str not in block or "시행" not in block:
            continue

        if re.search(r"⊙법률 제\d+호", block):
            return "", None

        meta_match = _META_LINE_RE.search(block)
        metadata = None
        if meta_match:
            metadata = {
                "law_number": meta_match.group(1),
                "amendment_date_str": meta_match.group(2).strip(),
                "amendment_type": meta_match.group(3).strip(),
            }

        reason_start = block.find("【제정·개정이유】")
        if reason_start == -1:
            return "", metadata
        reason_text = block[reason_start + len("【제정·개정이유】") :].strip()
        return reason_text, metadata

    return "", None


def fetch_revision_html(meta: LawChangeMeta) -> str | None:
    """법령/행정규칙의 제정·개정이유 HTML을 가져온다."""
    if meta.law_type == "ls" and meta.lsi_seq:
        # 예: lsInfoP.do?lsiSeq=255535&viewCls=lsRvsDocInfoR
        url = f"https://www.law.go.kr/lsInfoP.do?lsiSeq={meta.lsi_seq}&viewCls=lsRvsDocInfoR"
    elif meta.law_type == "admrul" and meta.admrul_seq:
        # 행정규칙: 기본 정보 페이지 (개정이유 포함)
        url = f"https://www.law.go.kr/admRulLsInfoP.do?admRulSeq={meta.admrul_seq}"
    else:
        return None

    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def fetch_old_new_html(meta: LawChangeMeta) -> str | None:
    """법령/행정규칙의 신·구조문 대비표 XML을 가져온다."""
    oc = _get_oc()

    if meta.law_type == "ls" and meta.lsi_seq:
        # 법령 신구법비교: target=oldAndNew, MST=법령일련번호
        url = (
            "http://www.law.go.kr/DRF/lawService.do"
            f"?OC={oc}&target=oldAndNew&MST={meta.lsi_seq}&type=XML"
        )
    elif meta.law_type == "admrul" and meta.admrul_seq:
        # 행정규칙 신구법비교: target=admrulOldAndNew, ID=행정규칙일련번호
        url = (
            "http://www.law.go.kr/DRF/lawService.do"
            f"?OC={oc}&target=admrulOldAndNew&ID={meta.admrul_seq}&type=XML"
        )
    else:
        return None

    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text

