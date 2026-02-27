from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Sequence


LawCategory = Literal["법령", "행정규칙", "입법예고"]
ChangeType = Literal["공포", "시행", "일부개정", "전부개정", "정정", "입법예고", "기타"]


@dataclass
class LawChangeMeta:
    """법령/행정규칙/입법예고 변경 메타데이터."""

    law_name: str
    category: LawCategory
    change_type: ChangeType
    announcement_date: date | None = None
    effective_date: date | None = None
    source: str | None = None
    detail_url: str | None = None
    law_id: str | None = None  # 국가법령정보센터 ID(lsId, lsRvsRsnListP.do용)
    chr_cls_cd: str | None = None   # 개정구분코드(예: 010202), lsRvsRsnListP.do용
    law_number: str | None = None   # 법률 제X호의 X (표시용, lsRvsRsnListP에서 파싱)
    amendment_date_str: str | None = None  # 공포일 표시 문자열 (예: "2023. 10. 24.")
    amendment_type: str | None = None  # 일부개정, 타법개정 등 (표시용)
    # 상세 조회용 식별자
    law_type: Literal["ls", "admrul"] | None = None
    lsi_seq: str | None = None       # 법령 일련번호 (lsInfoP용)
    admrul_seq: str | None = None    # 행정규칙 일련번호 (admRulLsInfoP, admrulOldAndNew용)


@dataclass
class ArticleComparisonRow:
    """신·구조문 대비표 한 행."""

    article_no: str | None
    article_title: str | None
    old_text: str | None
    new_text: str | None


@dataclass
class LawChangeDetail:
    """개정이유/주요 개정사항/신·구조문 대비표까지 포함한 상세 정보."""

    meta: LawChangeMeta
    reason_sections: list[str] = field(default_factory=list)
    main_change_sections: list[str] = field(default_factory=list)
    combined_reason_and_main_sections: list[str] = field(default_factory=list)
    article_comparisons: list[ArticleComparisonRow] = field(default_factory=list)

    def has_any_content(self) -> bool:
        return any(
            [
                self.reason_sections,
                self.main_change_sections,
                self.combined_reason_and_main_sections,
                self.article_comparisons,
            ]
        )


LawChangeDetailSeq = Sequence[LawChangeDetail]

