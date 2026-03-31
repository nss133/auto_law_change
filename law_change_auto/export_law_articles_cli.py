"""
국가법령정보센터 OPEN API로 현행 법령 본문을 받아 comp_matching_auto 용 CSV/엑셀을 생성한다.

사전 준비:
  - open.law.go.kr 에서 발급한 OC를 환경변수 LAW_GO_API_KEY 로 설정
  - 이 패키지 루트 상위(저장소 루트)의 .env 에 넣어도 됨

실행 예시 (저장소 루트에서):
  python -m law_change_auto.export_law_articles_cli --name \"개인정보 보호법\" -o output/pipa_articles.csv
  python -m law_change_auto.export_law_articles_cli --mst 259119 -o out.csv
  python -m law_change_auto.export_law_articles_cli --monitored -o output/all_monitored.csv
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# inner law_change_auto/ 의 상위 = 저장소 루트 (requirements.txt, data/ 있는 곳)
_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env")

from law_change_auto.config.monitored_laws_loader import load_monitored_laws
from law_change_auto.exporters.comp_matching_law_export import (
    law_api_payload_to_comp_matching_rows,
    write_comp_matching_csv,
    write_comp_matching_csv_excel,
    write_comp_matching_sqlite,
)
from law_change_auto.fetchers.law_body_fetcher import (
    fetch_law_body_by_name,
    fetch_law_body_json,
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="법령 본문(API) → law_name, article_ref, text CSV (comp_matching_auto 입력용)",
    )
    src = p.add_mutually_exclusive_group(required=False)
    src.add_argument("--mst", type=str, default="", help="법령일련번호(MST/lsiSeq)")
    src.add_argument("--law-id", type=str, default="", dest="law_id", help="법령 ID")
    src.add_argument("--name", type=str, default="", help="법령명 (검색 후 첫 건 본문)")
    src.add_argument(
        "--monitored",
        action="store_true",
        help="data/monitored_laws.xlsx 의 법령명마다 본문 수집 후 하나의 파일로 병합",
    )
    p.add_argument(
        "-o",
        "--output",
        type=str,
        required=True,
        help="출력 경로 (.csv, .xlsx, .sqlite)",
    )
    p.add_argument(
        "--monitored-path",
        type=str,
        default="data/monitored_laws.xlsx",
        help="--monitored 시 엑셀 경로 (저장소 루트 기준)",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=0.35,
        help="연속 API 호출 사이 대기(초). 기본 0.35",
    )
    p.add_argument(
        "--include-structure",
        action="store_true",
        help="편·장·절·관 제목 행도 포함(기본은 순수 조문만)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out = Path(args.output)
    only_jo = not args.include_structure

    if args.monitored:
        mon_path = _REPO_ROOT / args.monitored_path
        laws = load_monitored_laws(mon_path)
        all_rows: list[dict[str, str]] = []
        for i, law in enumerate(laws):
            print(f"[{i + 1}/{len(laws)}] {law.name} …", flush=True)
            try:
                payload = fetch_law_body_by_name(law.name)
                rows = law_api_payload_to_comp_matching_rows(
                    payload, only_jo=only_jo
                )
                all_rows.extend(rows)
                print(f"         → 조문 {len(rows)}행")
            except Exception as e:
                print(f"         [건너뜀] {e}", flush=True)
            time.sleep(args.sleep)
        if out.suffix.lower() == ".sqlite":
            write_comp_matching_sqlite(
                all_rows,
                out,
                source="law_change_auto:export_law_articles_cli:monitored",
            )
        elif out.suffix.lower() == ".xlsx":
            write_comp_matching_csv_excel(all_rows, out)
        else:
            write_comp_matching_csv(all_rows, out)
        print(f"완료: {out.resolve()} (총 {len(all_rows)}행)")
        return 0

    if args.mst:
        payload = fetch_law_body_json(mst=args.mst.strip())
    elif args.law_id:
        payload = fetch_law_body_json(law_id=args.law_id.strip())
    elif args.name:
        payload = fetch_law_body_by_name(args.name.strip())
    else:
        print(
            "오류: --mst, --law-id, --name, --monitored 중 하나를 지정하세요.",
            file=sys.stderr,
        )
        return 2

    rows = law_api_payload_to_comp_matching_rows(payload, only_jo=only_jo)
    if out.suffix.lower() == ".sqlite":
        write_comp_matching_sqlite(
            rows,
            out,
            source="law_change_auto:export_law_articles_cli",
        )
    elif out.suffix.lower() == ".xlsx":
        write_comp_matching_csv_excel(rows, out)
    else:
        write_comp_matching_csv(rows, out)
    print(f"완료: {out.resolve()} (조문 {len(rows)}행)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
