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

## 사용법

### 환경변수

- `LAW_GO_API_KEY`: 국가법령정보센터 API OC(이메일 ID). [open.law.go.kr](https://open.law.go.kr)에서 발급.
- `GEMINI_API_KEY`: **파급효과** 문구 생성용(권장). [Google AI Studio](https://aistudio.google.com/apikey)에서 발급. 없으면 기본 문구만 사용합니다.

파급효과 문단은 **Gemini API**로 생성하고, 호출 실패 시 **기본 문구**로 둡니다. 생명보험회사 임직원 독자·-음/-임 문체로 작성하도록 프롬프트되어 있습니다.

### CLI

1. `data/monitored_laws.xlsx`에 모니터링할 법령명 입력
2. 실행:
   ```bash
   python -m law_change_auto.cli --date 2024-10-25 --law 보험업법
   ```
3. `output/` 폴더에 **`목차.docx`**와 **`1. {법령명} 시행 안내.docx`** 등 번호 붙은 파일이 생성됩니다(건수만큼). 기간 모드(`--date-from`/`--date-to`)와 동일한 파일명 규칙입니다.

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

