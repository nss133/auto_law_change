"""law_change_auto/parsers/content_splitter.py 단위 테스트."""
from __future__ import annotations

import pytest

from law_change_auto.parsers.content_splitter import split_reason_and_main


class TestSplitReasonAndMain:
    def test_numbered_pattern(self):
        """'1. 개정이유 / 2. 주요내용' 패턴 분리."""
        text = "1. 개정이유\n이런저런 이유로 개정함.\n2. 주요내용\n핵심 변경 사항 기술."
        reason, main = split_reason_and_main(text)
        assert "이런저런 이유로 개정함" in reason
        assert "핵심 변경 사항 기술" in main

    def test_korean_alpha_pattern(self):
        """'가. 개정이유 / 나. 주요내용' 패턴 분리."""
        text = "가. 개정이유\n이유 텍스트.\n나. 주요내용\n내용 텍스트."
        reason, main = split_reason_and_main(text)
        assert "이유 텍스트" in reason
        assert "내용 텍스트" in main

    def test_integrated_text_no_separator(self):
        """개정이유/주요내용 구분 없는 통합 텍스트 → reason에 전체 내용."""
        text = "이 법령은 관련 규정을 정비하기 위해 개정합니다. 세부 내용은 아래와 같습니다."
        reason, main = split_reason_and_main(text)
        # 구분자 없으면 reason에 전체가, main은 비어있음
        assert reason != ""
        assert main == ""

    def test_tail_pattern_removed_uigyeon(self):
        """'의견 제출' 꼬리 패턴 제거."""
        text = "1. 개정이유\n이유 설명.\n2. 주요내용\n주요 내용.\n다. 의견 제출\n의견은 여기에 제출하세요."
        reason, main = split_reason_and_main(text)
        assert "의견 제출" not in main
        assert "의견은 여기에" not in main

    def test_empty_string(self):
        """빈 문자열 입력 → 빈 튜플."""
        reason, main = split_reason_and_main("")
        assert reason == ""
        assert main == ""

    def test_only_reason_header(self):
        """'1. 개정이유'만 있고 주요내용 헤더 없음 → reason에 내용, main 비어있음."""
        text = "1. 개정이유\n이유만 있는 텍스트."
        reason, main = split_reason_and_main(text)
        assert "이유만 있는 텍스트" in reason
        assert main == ""

    def test_only_main_header(self):
        """'2. 주요내용'만 있고 개정이유 헤더 없음 → main에 내용."""
        text = "배경 설명.\n2. 주요내용\n주요 내용 기술."
        reason, main = split_reason_and_main(text)
        assert "주요 내용 기술" in main

    def test_header_announcement_stripped(self):
        """'공고합니다.' 공고문 헤더가 있으면 제거 후 분리."""
        text = "◎ 금융위원회 공고합니다.\n1. 개정이유\n이유.\n2. 주요내용\n내용."
        reason, main = split_reason_and_main(text)
        assert "이유" in reason
        assert "내용" in main
        assert "공고합니다" not in reason

    def test_integrated_header_removed(self):
        """'개정이유 및 주요내용' 통합 제목은 제거 후 하위 패턴으로 분리."""
        text = "개정이유 및 주요내용\n가. 개정이유\n이유 설명.\n나. 주요내용\n내용 설명."
        reason, main = split_reason_and_main(text)
        assert "이유 설명" in reason
        assert "내용 설명" in main

    def test_return_type_is_tuple_of_str(self):
        """반환 타입이 (str, str) 튜플."""
        result = split_reason_and_main("테스트 텍스트")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], str)
        assert isinstance(result[1], str)
