# legal_doc_converter 형식 매핑

입법예고/규정변경예고 출력을 legal_doc_converter 문단·형식에 맞추기 위한 비교 정리.

---

## 1. legal_doc_converter 문서 구조 (입법예고/규정변경예고)

| 순서 | 섹션 | 비고 |
|------|------|------|
| 1 | 제목 | {법령명} 입법예고 안내 / 고시 규정변경예고 안내 |
| 2 | 메타 | [시행/예고일], 날짜 우측정렬, 부서 |
| 3 | **1. 개정이유** | reason_paragraphs (문단 리스트) |
| 4 | **2. 주요내용** | main_contents_from_txt (문단 리스트) |
| 5 | **3. 의견제출기한** | opinion_deadline (예: 2026. 4. 7.) — 있으면만 |
| 6 | **4. 파급효과** | impact_text (굵게) |
| 7 | **5. 신구조문 대비표** | comparison_table |

- **is_combined_format = False**: 개정이유와 주요내용을 **항상 분리** (1. 개정이유, 2. 주요내용)
- **parser**: 1.개정이유 ~ 2.주요내용, 2.주요내용 ~ 3.의견제출 구간으로 분리
- **의견제출기한**: 3. 의견제출 구간에서 날짜(예: 2026년 4월 7일) 추출 → `2026. 4. 7.` 형식

---

## 2. legal_doc_converter docx_generator 형식

| 항목 | 값 |
|------|-----|
| LINE_SPACING_BODY | 1.0 |
| LINE_SPACING_TITLE | 1.0 |
| PARAGRAPH_SPACING_BEFORE | Pt(0) |
| PARAGRAPH_SPACING_AFTER | Pt(0) |
| 파급효과 | run.font.size = Pt(10) |

---

## 3. law_change_auto 현재 vs legal_doc_converter

| 항목 | law_change_auto 현재 | legal_doc_converter |
|------|---------------------|---------------------|
| 개정이유·주요내용 | 합쳐진 경우 "1. 개정이유 및 주요내용" | 항상 "1. 개정이유" + "2. 주요내용" 분리 |
| 의견제출기한 | 없음 | "3. 의견제출기한" 섹션 있음 |
| LINE_SPACING_BODY | 1.5 | 1.0 |
| PARAGRAPH_SPACING_AFTER | Pt(6) | Pt(0) |
| 파급효과 폰트 | 11pt | 10pt |

---

## 4. 적용 작업 (입법예고/규정변경예고용)

1. **legislation_parser**: PDF 텍스트를 1.개정이유 / 2.주요내용 / 3.의견제출 구간으로 분리, opinion_deadline 추출
2. **docx_generator**: 입법예고일 때 legal_doc_converter 구조 적용
   - 1. 개정이유
   - 2. 주요내용
   - 3. 의견제출기한 (opinion_deadline 있을 때)
   - 4. 파급효과
   - 5. 신구조문 대비표
3. **형식**: 입법예고 전용 시 LINE_SPACING_BODY=1.0, PARAGRAPH_SPACING_AFTER=0, 파급효과 10pt
