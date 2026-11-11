from datetime import date
import unicodedata

from law_change_auto.models import LawChangeDetail, LawChangeMeta
from law_change_auto.services.publication import classify, group_effective, safe_filename
from law_change_auto.docx_generator.generator import guide_display_title

START, END = date(2026, 9, 1), date(2026, 10, 31)

def detail(name='보험업법', effective=START, category='법령', change='시행'):
    return LawChangeDetail(LawChangeMeta(name, category, change,
        announcement_date=START, effective_date=effective))

def test_classification_preserves_different_events():
    assert classify(detail(), START, END) == '시행'
    assert classify(detail(effective=date(2027, 3, 9)), START, END) == '향후 시행'
    assert classify(detail(category='입법예고', change='입법예고'), START, END) == '예고'
    assert classify(detail(effective=date(2021, 10, 9)), START, END) == '공포·발령'
    assert classify(detail(change='한시조문'), START, END) == '별도 확인'

def test_family_group_retains_every_version():
    ds = [detail(), detail('보험업법 시행령'), detail('보험업법', date(2026, 10, 1)),
          detail('상법', date(2027, 1, 1))]
    groups = group_effective(ds, START, END)
    assert len(groups) == 1
    assert list(groups.values())[0] == ds[:3]

def test_renamed_law_groups_under_same_monitored_name():
    from law_change_auto.config.monitored_laws_loader import MonitoredLaw
    old = '전기통신금융사기 피해 방지 및 피해금 환급에 관한 특별법'
    new = old.replace('피해금', '피해자산')
    groups = group_effective([detail(old), detail(new)], START, END, [MonitoredLaw(old)])
    assert len(groups) == 1

def test_filename_nfd_limit_and_collision():
    used = set()
    names = [safe_filename('1. ' + '금융감독규정'*40, used) for _ in range(2)]
    assert names[0] != names[1]
    assert all(len(unicodedata.normalize('NFD', n).encode()) <= 240 for n in names)

def test_titles_do_not_label_promulgation_as_enforcement_or_notice():
    assert '공포 안내' in guide_display_title(detail(change='공포').meta)
    title = guide_display_title(detail(category='행정규칙', change='공포').meta)
    assert '예고' not in title
