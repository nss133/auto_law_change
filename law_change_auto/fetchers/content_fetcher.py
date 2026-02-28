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

def fetch_revision_reason_from_ls_rvs_rsn_list(
    ls_id: str,
    chr_cls_cd: str,
    target_date_str: str,
) -> tuple[str, dict | None]:
    \"\"\"
    OpenAPI target=eflaw 를 사용하여 제·개정이유를 가져온다.
    기존 lsRvsRsnListP.do 크롤링 방식은 레이아웃 변화에 취약하므로 OpenAPI를 사용하도록 수정.
    \"\"\"
    # 날짜 포맷 변환 (예: \"2024. 10. 25.\" -> \"20241025\")
    ef_yd = re.sub(r\"[^0-9]\", \"\", target_date_str)
    
    oc = _get_oc()
    # eflaw API는 ID(ls_id)와 MST(lsi_seq)를 모두 지원하며, 
    # 제개정이유내용(제·개정이유) 및 개정문내용(신구조문대비표 포함) 필드를 제공한다.
    url = (
        \"http://www.law.go.kr/DRF/lawService.do\"
        f\"?OC={oc}&target=eflaw&ID={ls_id}&efYd={ef_yd}&chrClsCd={chr_cls_cd}&type=XML\"
    )
    
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.encoding = \"utf-8\"
        soup = BeautifulSoup(resp.text, \"xml\")
        
        reason_tag = soup.find(\"제개정이유내용\")
        reason_text = reason_tag.get_text(strip=True) if reason_tag else \"\"
        
        # 메타데이터 추출 (기존 모델과 호환성 유지)
        metadata = None
        law_num = soup.find(\"공포번호\")
        pub_date = soup.find(\"공포일자\")
        amd_type = soup.find(\"제개정구분\")
        
        if law_num and pub_date and amd_type:
            pub_date_val = pub_date.get_text(strip=True)
            # 날짜 형식 보정 (YYYYMMDD -> YYYY. M. D.)
            if len(pub_date_val) == 8:
                y, m, d = pub_date_val[:4], int(pub_date_val[4:6]), int(pub_date_val[6:])
                pub_date_val = f\"{y}. {m}. {d}.\"
                
            metadata = {
                \"law_number\": law_num.get_text(strip=True),
                \"amendment_date_str\": pub_date_val,
                \"amendment_type\": amd_type.get_text(strip=True),
            }
        
        return reason_text, metadata
    except Exception:
        return \"\", None

def fetch_revision_html(meta: LawChangeMeta) -> str | None:
    \"\"\"법령/행정규칙의 제정·개정이유 HTML을 가져온다.\"\"\"
    if meta.law_type == \"ls\" and meta.lsi_seq:
        url = f\"https://www.law.go.kr/lsInfoP.do?lsiSeq={meta.lsi_seq}&viewCls=lsRvsDocInfoR\"
    elif meta.law_type == \"admrul\" and meta.admrul_seq:
        url = (
            \"https://www.law.go.kr/admRulInfoP.do\"
            f\"?admRulSeq={meta.admrul_seq}&urlMode=admRulRvsInfoR\"
        )
    else:
        return None

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = \"utf-8\"
        return resp.text
    except Exception:
        return None

def fetch_old_new_html(meta: LawChangeMeta) -> str | None:
    \"\"\"법령/행정규칙의 신·구조문 대비표 XML을 가져온다.\"\"\"
    oc = _get_oc()
    if meta.law_type == \"ls\" and meta.lsi_seq:
        url = (
            \"http://www.law.go.kr/DRF/lawService.do\"
            f\"?OC={oc}&target=oldAndNew&MST={meta.lsi_seq}&type=XML\"
        )
    elif meta.law_type == \"admrul\" and meta.admrul_seq:
        url = (
            \"http://www.law.go.kr/DRF/lawService.do\"
            f\"?OC={oc}&target=admrulOldAndNew&ID={meta.admrul_seq}&type=XML\"
        )
    else:
        return None

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = \"utf-8\"
        return resp.text
    except Exception:
        return None
