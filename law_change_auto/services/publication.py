"""Deterministic publication policy; collection never implies individual issuance."""
from __future__ import annotations

import re
import unicodedata
from datetime import date

from ..matching.law_matcher import _normalize_name, match_laws


def classify(detail, start: date, end: date) -> str:
    m = detail.meta
    if m.calendar_event_date and m.change_type == '기타':
        return '별도 확인'
    if '한시' in m.change_type or '폐지' in m.change_type:
        return '별도 확인'
    if m.category == '입법예고':
        return '예고'
    if m.effective_date and start <= m.effective_date <= end:
        return '시행'
    if m.announcement_date and start <= m.announcement_date <= end:
        return '향후 시행' if m.effective_date and m.effective_date > end else '공포·발령'
    return '별도 확인'


def group_effective(details, start: date, end: date, monitored_laws=None) -> dict:
    groups = {}
    for d in details:
        if classify(d, start, end) == '시행':
            matches = match_laws(monitored_laws, [d.meta], threshold=0.9) if monitored_laws else []
            family = matches[0].monitored.name if matches else d.meta.law_name
            groups.setdefault(_normalize_name(family), []).append(d)
    return groups


def safe_filename(title: str, used: set, extension: str = '.docx') -> str:
    """Allow room below 255 bytes even on NFD-normalizing filesystems."""
    base = unicodedata.normalize('NFC', re.sub(r'[\"*/:<>?\\|]', '_', title)).strip()
    n = 1
    while True:
        suffix = (f'_{n}' if n > 1 else '') + extension
        stem = base
        while len(unicodedata.normalize('NFD', stem + suffix).encode('utf-8')) > 240:
            stem = stem[:-1]
        name = stem + suffix
        key = unicodedata.normalize('NFC', name)
        if key not in used:
            used.add(key)
            return name
        n += 1
