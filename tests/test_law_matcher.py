"""law_change_auto/matching/law_matcher.py 단위 테스트."""
from __future__ import annotations

from datetime import date

import pytest

from law_change_auto.matching.law_matcher import _normalize_name, match_laws
from law_change_auto.config.monitored_laws_loader import MonitoredLaw
from law_change_auto.models import LawChangeMeta


# ---------------------------------------------------------------------------
# _normalize_name
# ---------------------------------------------------------------------------

class TestNormalizeName:
    def test_removes_parentheses_and_content(self):
        """괄호와 그 안의 내용을 제거."""
        result = _normalize_name("금융투자업규정(금융위원회고시)")
        assert "(" not in result
        assert ")" not in result
        assert "금융위원회고시" not in result
        assert "금융투자업규정" in result

    def test_removes_sihaengryeong_suffix(self):
        """'시행령' 접미어 제거 → 모법과 동일 정규화 결과."""
        assert _normalize_name("보험업법시행령") == _normalize_name("보험업법")

    def test_removes_sihaenggyuchig_suffix(self):
        """'시행규칙' 접미어 제거."""
        assert _normalize_name("자본시장법시행규칙") == _normalize_name("자본시장법")

    def test_removes_whitespace(self):
        """공백 제거."""
        result = _normalize_name("보험 업 법")
        assert " " not in result

    def test_removes_special_characters(self):
        """특수문자(·, -, _) 제거."""
        a = _normalize_name("보험업법·시행령")
        b = _normalize_name("보험업법시행령")
        # 둘 다 같은 정규화 결과 (시행령 제거 후 동일)
        assert a == b

    def test_lowercase(self):
        """소문자 변환 (영문 포함 법령명)."""
        result = _normalize_name("ABC법")
        assert result == result.lower()

    def test_empty_string(self):
        """빈 문자열 입력 → 빈 문자열 반환."""
        assert _normalize_name("") == ""

    def test_none_like_empty(self):
        """None이 아닌 빈 문자열 처리."""
        assert _normalize_name("  ") == ""


# ---------------------------------------------------------------------------
# match_laws
# ---------------------------------------------------------------------------

def _make_meta(law_name: str, url: str = "http://example.com") -> LawChangeMeta:
    return LawChangeMeta(
        law_name=law_name,
        category="법령",
        change_type="일부개정",
        announcement_date=date(2026, 1, 1),
        detail_url=url,
    )


def _make_monitored(name: str) -> MonitoredLaw:
    return MonitoredLaw(name=name)


class TestMatchLaws:
    def test_exact_match_score_one(self):
        """동일 법령명 → score == 1.0."""
        monitored = [_make_monitored("보험업법")]
        metas = [_make_meta("보험업법")]
        results = match_laws(monitored, metas, threshold=0.8)
        assert len(results) == 1
        assert results[0].score == pytest.approx(1.0)

    def test_similar_name_match(self):
        """유사 법령명 → threshold 0.8 이상에서 매칭."""
        monitored = [_make_monitored("자본시장과금융투자업에관한법률")]
        # 괄호 부제목이 붙은 변형
        metas = [_make_meta("자본시장과금융투자업에관한법률(자본시장법)")]
        results = match_laws(monitored, metas, threshold=0.8)
        assert len(results) == 1
        assert results[0].score >= 0.8

    def test_below_threshold_no_match(self):
        """유사도가 threshold 미달 → 매칭 없음."""
        monitored = [_make_monitored("보험업법")]
        metas = [_make_meta("전혀다른법률명XXXXXXX")]
        results = match_laws(monitored, metas, threshold=0.8)
        assert len(results) == 0

    def test_empty_metas(self):
        """metas 빈 리스트 → 결과 없음."""
        monitored = [_make_monitored("보험업법")]
        results = match_laws(monitored, [], threshold=0.8)
        assert results == []

    def test_empty_monitored(self):
        """monitored_laws 빈 리스트 → 결과 없음."""
        metas = [_make_meta("보험업법")]
        results = match_laws([], metas, threshold=0.8)
        assert results == []

    def test_both_empty(self):
        """양쪽 모두 빈 리스트 → 빈 결과."""
        assert match_laws([], [], threshold=0.8) == []

    def test_sihaengryeong_matches_parent_law(self):
        """시행령은 모법과 매칭됨 (접미어 제거 정규화 덕분)."""
        monitored = [_make_monitored("보험업법")]
        metas = [_make_meta("보험업법 시행령")]
        results = match_laws(monitored, metas, threshold=0.8)
        assert len(results) == 1

    def test_returns_match_result_fields(self):
        """MatchResult에 meta, monitored, score 필드 존재."""
        monitored = [_make_monitored("보험업법")]
        metas = [_make_meta("보험업법")]
        results = match_laws(monitored, metas)
        r = results[0]
        assert r.meta.law_name == "보험업법"
        assert r.monitored.name == "보험업법"
        assert isinstance(r.score, float)

    def test_multiple_metas_partial_match(self):
        """여러 meta 중 일부만 매칭."""
        monitored = [_make_monitored("보험업법")]
        metas = [
            _make_meta("보험업법", url="http://a.com"),
            _make_meta("전혀다른법률YYYYYYY", url="http://b.com"),
        ]
        results = match_laws(monitored, metas, threshold=0.8)
        assert len(results) == 1
        assert results[0].meta.detail_url == "http://a.com"


class TestShortNameFalsePositives:
    """짧은 법령명 1음절 차이 오탐 방지 (국어기본법↔국세기본법 등)."""

    @pytest.mark.parametrize(
        "monitored_name, fp_name",
        [
            ("국세기본법", "국어기본법"),   # 1글자(어↔세)
            ("국세기본법", "국토기본법"),   # 1글자(토↔세)
            ("상법", "기상법 시행령"),       # '상법'⊂'기상법'
            ("상법", "상표법 시행규칙"),     # 1글자
            ("지방세법", "지방세징수법 시행규칙"),
            ("지방세법", "지방세기본법 시행령"),
        ],
    )
    def test_short_name_one_syllable_diff_not_matched(self, monitored_name, fp_name):
        """짧은 이름은 0.8을 넘어도 완전일치가 아니면 매칭하지 않음."""
        results = match_laws(
            [_make_monitored(monitored_name)], [_make_meta(fp_name)], threshold=0.8
        )
        assert results == []

    def test_short_name_exact_still_matches(self):
        """짧은 이름이라도 완전일치(시행령 접미어 포함)는 매칭."""
        results = match_laws(
            [_make_monitored("국세기본법")],
            [_make_meta("국세기본법 시행령")],
            threshold=0.8,
        )
        assert len(results) == 1

    def test_long_name_fuzzy_still_matches(self):
        """긴 이름의 사소한 변형(1글자 오타)은 fuzzy로 계속 매칭."""
        results = match_laws(
            [_make_monitored("독점규제 및 공정거래에 관한 법률")],
            [_make_meta("독점규제 및 공정거래에 관한 법뮬")],  # 律→뮬 오타
            threshold=0.8,
        )
        assert len(results) == 1
        assert results[0].score >= 0.8
