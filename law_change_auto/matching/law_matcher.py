from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

from Levenshtein import ratio as levenshtein_ratio

from ..config.monitored_laws_loader import MonitoredLaw
from ..models import LawChangeMeta


def _normalize_name(name: str) -> str:
    """법령명 비교를 위한 정규화.

    - 괄호(부제목) 제거
    - 공백·특수기호 제거
    - '시행령', '시행규칙' 접미어 제거 → 모법과 하위법규를 한 묶음으로 취급
    - 소문자 변환
    """
    if not name:
        return ""
    # 괄호 안 부제목 제거
    name = re.sub(r"\(.*?\)", "", name)
    # 공백·특수문자 제거
    name = re.sub(r"[\s·ㆍ\-_/]", "", name)
    # 하위법규 접미어 제거 (예: 보험업법시행령 → 보험업법)
    name = re.sub(r"(시행령|시행규칙)$", "", name)
    return name.lower()


@dataclass
class MatchResult:
    meta: LawChangeMeta
    monitored: MonitoredLaw
    score: float


def match_laws(
    monitored_laws: List[MonitoredLaw],
    metas: List[LawChangeMeta],
    threshold: float = 0.8,
) -> List[MatchResult]:
    """모니터링 대상 법령명과 수집한 법령 메타데이터를 유사도 기반으로 매칭."""
    results: List[MatchResult] = []

    normalized_monitored = [
        (m, _normalize_name(m.name)) for m in monitored_laws if m.name.strip()
    ]

    for meta in metas:
        norm_meta = _normalize_name(meta.law_name)
        if not norm_meta:
            continue

        best_match: Tuple[MonitoredLaw | None, float] = (None, 0.0)
        for monitored, norm_name in normalized_monitored:
            if not norm_name:
                continue
            score = levenshtein_ratio(norm_meta, norm_name)
            if score > best_match[1]:
                best_match = (monitored, score)

        monitored, best_score = best_match
        if monitored is not None and best_score >= threshold:
            results.append(MatchResult(meta=meta, monitored=monitored, score=best_score))

    return results


def augment_fsc_legislation_matches(
    monitored_laws: List[MonitoredLaw],
    in_range: List[LawChangeMeta],
    matches: List[MatchResult],
    *,
    min_norm_len: int = 12,
) -> List[MatchResult]:
    """금융위 통합 공지(한 줄에 시행령·규정 등 복수 건)처럼 전체 제목만으로는 유사도가 낮을 때 보강.

    정규화한 모니터링 법령명이 공지 제목(정규화)에 **부분 문자열**로 들어가면 매칭에 포함한다.
    """
    if not in_range:
        return matches
    matched_urls = {m.meta.detail_url for m in matches if m.meta.detail_url}
    out: List[MatchResult] = list(matches)

    for meta in in_range:
        url = meta.detail_url
        if not url or url in matched_urls:
            continue
        norm_title = _normalize_name(meta.law_name)
        for law in monitored_laws:
            nn = _normalize_name(law.name)
            if len(nn) < min_norm_len:
                continue
            if nn in norm_title:
                out.append(MatchResult(meta=meta, monitored=law, score=0.52))
                matched_urls.add(url)
                break

    return out

