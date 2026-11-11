# 법령제·개정 안내서 자동 생성기

macOS 데스크톱에서 매일 자동으로 국가법령정보센터(법제처) 기준 **법령/행정규칙/입법예고** 변경 사항을 조회하고,  
모니터링 대상 법령 리스트에 해당하는 건에 대해 **법령제·개정 안내서(docx)**를 생성하는 Python 앱입니다.

## 주요 기능

- 국가법령정보센터(`https://www.law.go.kr/main.html`) 기준
  - 법령(법률·대통령령·부령)
  - 행정규칙(훈령·예규·고시)
- 지정한 법령 리스트(`data/monitored_laws.xlsx`)에 해당하는 변경·예고 자동 탐지
- 개정이유, 주요 개정사항(또는 개정이유+주요내용 통합 섹션), 신·구조문 대비표 파싱
- Word(`.docx`) 형식의 `법령제·개정 안내서` 자동 생성

## 필요 환경

- Python 3.10 이상 권장
- 의존성 설치:

```bash
pip install -r requirements.txt
```

개발·테스트용 의존성까지 설치하려면:

```bash
pip install -r requirements-dev.txt
```

## 사용법

### 생성·검수·배포 정책 (2026-11-11 수정)

프로젝트 작업 지침은 [AGENTS.md](AGENTS.md), 상세 실행 계약은
[DELIVERY_HARNESS.md](docs/DELIVERY_HARNESS.md)를 따릅니다.

기간·단일 날짜·예고 모드는 [후보 선별 기준](docs/CANDIDATE_SELECTION.md)에 따라 **우선 안내로 선정된 개정 버전별 개별 안내서 + 목차 + 종합요약**을 생성합니다.
전체 탐지 후보는 원문·선별 사유와 함께 별도 목록에 보존합니다. 제목 키워드만으로 중요도를 올리지 않으며 적용 여부 확인·참고·원문 보류는 자동 발행하지 않습니다.
사용자가 요약만 요청한 경우 --digest-only, 시행 법령군별 병합을 요청한 경우
--detailed-guides를 명시적으로 선택합니다. 이 두 옵션은 동시에 사용할 수 없습니다.

    .venv/bin/python -m law_change_auto.cli --validate-env
    .venv/bin/python -m law_change_auto.cli --date-from 2026-10-01 --date-to 2026-11-30 --output-dir output/새로운_실행폴더

- 기존 폴더에 덮어쓰지 않습니다. 미래 종료일을 지정해도 실행일 이후 새 발표 자료는 포함되지 않습니다.
- 개별 DOCX·목차는 templates/guide_reference.docx, 요약문은 templates/summary_reference.docx의 사용자 지정 양식을 각각 보존합니다.
- 요약문은 개요 → 회사 관련 항목 선별표 → 전체 발행 목록표 → 미결사항을 유지합니다. 시행·예고 분류가 있어도 선별표를 생략하지 않으며 두 표의 번호·내용 일치를 검사합니다.
- --no-summary-llm은 AI 호출을 생략한 초안입니다. 파급효과 성공이나 배포 완료로 표시하지 않습니다.
- 상세 원문·신구조문·예외·시행일을 함께 입력하고 실제 응답 ID·모델·토큰·원문/프롬프트/응답 해시를 보존합니다.
- 응답 증거 없는 과거 캐시는 재사용하지 않습니다. 캐시 적중과 새 API 호출을 별도로 집계합니다.
- 원문 미확보·충돌·타법개정의 영향은 보류합니다. 기본 문구로 성공을 대신하지 않습니다.
- run_report.json은 수집·분류·생성·API 상태, delivery_manifest.json은 개별본 연결과 파일/원문 해시를 기록합니다.
- candidate_selection.json과 후보선별_검토목록.md는 전 후보의 분류·사유·원문 인용을 기록합니다. --selection-review로 실제 검토한 적용 조건을 반영할 수 있습니다. 발행 대상 0건이어도 후보 기록은 보존합니다.
- 생성 성공은 배포 완료가 아닙니다. 실제 원문 대조와 PDF/PNG 전 페이지 시각 검수를 수행한 뒤 검수 기록을 남깁니다.

배포 전 검사와 ZIP 생성:

    .venv/bin/python scripts/validate_delivery.py output/새로운_실행폴더 --review-template
    # 실제 검수 후 delivery_review.json 기록
    .venv/bin/python scripts/validate_delivery.py output/새로운_실행폴더
    .venv/bin/python scripts/validate_delivery.py output/새로운_실행폴더 --package output/새로운_배포본.zip

검수 누락·파일 변경·호출 증거 누락·양식 불일치이면 배포 검사는 종료 코드 1로 중단하며 ZIP을 만들지 않습니다.
검토 기록 작성법과 검증 한계는 상세 계약을 확인합니다.

실수집 검증에서 추가 보완한 사항:

- 행정규칙 API는 `efYd` 기간검색을 지원하지 않습니다. 발령일 `prmlYd` 조회와
  시행일 내림차순 `efdes` 조회를 병합하고 기간 시작일 이전에서 중단합니다.
  [공식 API 안내](https://open.law.go.kr/LSO/openApi/guideResult.do?htmlName=admrulListGuide)
- 타법개정은 다른 법령의 전체 개정이유가 섞일 수 있어 자동 요약을 보류하고 실제 변경조문 확인 대상으로 표시합니다.
- 명칭이 변경된 법령은 모니터링 목록의 동일 항목과 높은 유사도로 연결되는 경우 같은 법령군으로 묶습니다.
- `collected_details.json`은 수집 당시 원문·메타데이터 스냅샷입니다. 최종 요약에 실리지 않은 원문도 보존합니다.
- `scripts/validate_period_replay.py`는 2026년 9~10월 스냅샷 전용 검증 도구입니다.
  외부 AI 호출을 차단한 상태에서 캐시 재사용·기존 목록 대비 추가/누락·분류를 확인합니다.

### 환경변수

- `LAW_GO_API_KEY`: 국가법령정보센터 API OC(이메일 ID). [open.law.go.kr](https://open.law.go.kr)에서 발급.
- `GEMINI_API_KEY`: **파급효과** 문구 생성용(권장). [Google AI Studio](https://aistudio.google.com/apikey)에서 발급. 없으면 파급효과 생성을 보류하고 배포할 수 없습니다.

`.env.example`을 참고해 프로젝트 루트에 `.env` 파일을 만들 수 있습니다.

파급효과 초안은 Groq → Gemini 순서로 생성하며, 실제 응답 증거를 기록합니다. 실패 시 생성 성공으로 처리하지 않습니다. 생명보험회사 임직원 독자·-음/-임 문체를 사용하고, 원문 대조·시각 검수는 별도로 수행합니다.

### CLI

1. `data/monitored_laws.xlsx`에 모니터링할 법령명 입력
2. 실행:
   ```bash
   python -m law_change_auto.cli --date 2024-10-25 --law 보험업법
   ```
3. `output/` 폴더에 **`목차.docx`**와 **`1. {법령명} 시행 안내.docx`** 등 번호 붙은 파일이 생성됩니다(건수만큼). 기간 모드(`--date-from`/`--date-to`)와 동일한 파일명 규칙입니다.
4. 실행 후 `run_report.json`이 함께 생성되어 생성 파일, 법령별 매칭·파싱 건수, 신구조문 추출 건수를 확인할 수 있습니다.

환경과 모니터링 목록만 확인하려면:

```bash
python -m law_change_auto.cli --validate-env
python -m law_change_auto.cli --list-monitored
```

### 입법예고/규정변경예고 모드

금융위원회(FSC) 입법예고·규정변경예고 목록에서 매칭 건의 PDF 첨부를 추출해 안내서를 생성합니다.

```bash
python -m law_change_auto.cli --legislation --law 자본시장
# output/law_change_guide_legislation_<noticeId>.docx 생성
```

- `--legislation`: 입법예고 모드
- `--law`: 제목에 포함된 검색어 (예: 자본시장, 보험업)

### 사규 매칭용 법령 DB 번들 (comp_matching_auto)

`data/monitored_laws.xlsx`에 있는 법령명마다 **lsRlt(관련법령)** 로 법률·시행령·시행규칙을 묶어 확장한 뒤, 각각 현행 본문을 받아 형제 폴더 `comp_matching_auto/data/laws_monitored.sqlite`에 조문 단위로 저장합니다. (목록이 법률이면 시행령·시행규칙까지, 시행령이면 모법·시행규칙까지.) `LAW_GO_API_KEY` 및 [open.law.go.kr](https://open.law.go.kr) 허용 IP 등록이 필요합니다.

```bash
python -m law_change_auto.bundle_comp_matching_laws
# 출력 경로 지정: -o /path/to/laws.sqlite
# CSV 백업: --also-csv backup.csv
# 확장 없이 예전 방식만: --legacy-single
```

`comp_matching_auto` 쪽에서는 `./sync_laws_from_law_change_auto.sh`로 동일 작업을 호출할 수 있습니다.

### 특정 법령(lsiSeq) 기준 테스트

```bash
python run_lsi_255535.py
# output/law_change_guide_lsi_255535.docx 생성 (보험업법 2024-10-25 시행)
```

macOS에서는 `launchd`를 이용해 위 CLI를 매일 정해진 시각에 자동 실행하도록 설정할 수 있습니다.
