"""law_change_auto/parsers/legislation_parser.py 단위 테스트."""
from __future__ import annotations

import pytest

from law_change_auto.parsers.legislation_parser import parse_reason_main_from_notice_body


class TestParseReasonMainFromNoticeBody:
    def test_full_structure_all_sections(self):
        """개정이유/주요내용/의견제출기한 모두 포함된 텍스트."""
        text = (
            "1. 개정이유\n"
            "현행 법령의 미비점을 보완하고자 함.\n\n"
            "2. 주요내용\n"
            "가. 제3조 관련 규정 정비.\n"
            "나. 제5조 조항 신설.\n\n"
            "3. 의견 제출\n"
            "의견 제출 기한: 2026년 4월 30일\n\n"
        )
        reason_paras, main_paras, deadline = parse_reason_main_from_notice_body(text)
        assert len(reason_paras) >= 1
        assert len(main_paras) >= 1
        assert deadline == "2026. 4. 30."

    def test_no_opinion_deadline(self):
        """의견제출기한 없는 텍스트 → deadline is None."""
        text = (
            "1. 개정이유\n"
            "현행 규정의 미비점을 보완하고 관련 조항의 체계를 개선하고자 함.\n\n"
            "2. 주요내용\n"
            "가. 제1조 관련 규정을 정비하여 명확성을 제고함.\n"
        )
        reason_paras, main_paras, deadline = parse_reason_main_from_notice_body(text)
        assert len(reason_paras) >= 1
        assert deadline is None

    def test_only_reason(self):
        """개정이유만 있는 텍스트 → reason_paras 존재, main_paras 비어있음."""
        text = (
            "1. 개정이유\n"
            "이유만 있는 경우의 텍스트입니다. 상당히 긴 본문 내용을 포함합니다.\n"
        )
        reason_paras, main_paras, deadline = parse_reason_main_from_notice_body(text)
        # idx_main 없으면 main_block 추출 안 됨
        assert main_paras == []

    def test_empty_string(self):
        """빈 문자열 → 모두 빈 결과."""
        reason_paras, main_paras, deadline = parse_reason_main_from_notice_body("")
        assert reason_paras == []
        assert main_paras == []
        assert deadline is None

    def test_short_text_below_threshold(self):
        """50자 미만 짧은 텍스트 → 빈 결과 (길이 임계값)."""
        reason_paras, main_paras, deadline = parse_reason_main_from_notice_body("짧은 텍스트")
        assert reason_paras == []
        assert main_paras == []
        assert deadline is None

    def test_deadline_extracted_dotted_format(self):
        """의견제출기한을 'YYYY. M. D.' 형식으로 추출."""
        text = (
            "1. 개정이유\n"
            "규정 정비를 위한 개정으로 관련 내용을 수정하고자 함.\n\n"
            "2. 주요내용\n"
            "주요 개정 내용 기술.\n\n"
            "3. 의견 제출\n"
            "의견 제출은 2026. 5. 7.까지 가능합니다.\n"
        )
        _, _, deadline = parse_reason_main_from_notice_body(text)
        assert deadline == "2026. 5. 7."

    def test_reason_paras_is_list(self):
        """반환값이 (list, list, str|None) 타입."""
        text = (
            "1. 개정이유\n"
            "이유 텍스트가 있습니다. 충분한 길이의 본문입니다.\n\n"
            "2. 주요내용\n"
            "주요 내용 텍스트가 있습니다.\n"
        )
        reason_paras, main_paras, deadline = parse_reason_main_from_notice_body(text)
        assert isinstance(reason_paras, list)
        assert isinstance(main_paras, list)
        assert deadline is None or isinstance(deadline, str)

    def test_reason_paras_max_50(self):
        """reason_paras는 최대 50개 제한."""
        lines = "\n".join(f"가. 항목{i} 내용입니다." for i in range(100))
        text = f"1. 개정이유\n{lines}\n\n2. 주요내용\n주요 내용.\n"
        reason_paras, _, _ = parse_reason_main_from_notice_body(text)
        assert len(reason_paras) <= 50

    def test_main_paras_max_30(self):
        """main_paras는 최대 30개 제한."""
        lines = "\n".join(f"나. 항목{i} 내용입니다." for i in range(60))
        text = f"1. 개정이유\n이유 내용.\n\n2. 주요내용\n{lines}\n\n3. 의견 제출\n날짜 없음.\n"
        _, main_paras, _ = parse_reason_main_from_notice_body(text)
        assert len(main_paras) <= 30
