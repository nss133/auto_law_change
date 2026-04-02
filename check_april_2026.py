"""2026년 4월 1일~30일 수집건수 확인 스크립트."""
import datetime as dt
import os
import sys

# API 키 확인
if not os.getenv("LAW_GO_API_KEY"):
    print("오류: LAW_GO_API_KEY 환경변수가 설정되지 않았습니다.")
    sys.exit(1)

from law_change_auto.config.monitored_laws_loader import load_monitored_laws
from law_change_auto.fetchers.national_law_fetcher import (
    get_law_changes_for_monitored,
    get_recent_admin_rule_changes,
)
from law_change_auto.matching.law_matcher import match_laws

monitored_laws = load_monitored_laws()
print(f"모니터링 대상 법령 수: {len(monitored_laws)}\n")

start = dt.date(2026, 4, 1)
end   = dt.date(2026, 4, 30)

total_law = 0
total_admin = 0
total_matched = 0

print(f"{'날짜':<12} {'법령':>4} {'행정규칙':>6} {'매칭':>4}  매칭 법령명")
print("-" * 80)

day = start
while day <= end:
    try:
        law_metas   = get_law_changes_for_monitored(monitored_laws, day)
    except Exception as e:
        law_metas = []
        print(f"{day.isoformat():<12} API오류: {e}")
        day += dt.timedelta(days=1)
        continue

    try:
        admin_metas = get_recent_admin_rule_changes(day)
    except Exception:
        admin_metas = []

    all_metas = [*law_metas, *admin_metas]
    matches   = match_laws(monitored_laws, all_metas, threshold=0.8)

    names = ", ".join(m.meta.law_name for m in matches) if matches else "-"
    print(f"{day.isoformat():<12} {len(law_metas):>4} {len(admin_metas):>6} {len(matches):>4}  {names}")

    total_law   += len(law_metas)
    total_admin += len(admin_metas)
    total_matched += len(matches)

    day += dt.timedelta(days=1)

print("-" * 80)
print(f"{'합계':<12} {total_law:>4} {total_admin:>6} {total_matched:>4}")
