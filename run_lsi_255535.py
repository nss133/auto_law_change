from datetime import date
from pathlib import Path

from law_change_auto.fetchers.content_fetcher import (
    fetch_old_new_html,
    fetch_revision_html,
    fetch_revision_reason_from_ls_rvs_rsn_list,
)
from law_change_auto.models import LawChangeMeta
from law_change_auto.docx_generator.generator import generate_guide
from law_change_auto.parsers.law_change_parser import parse_law_change


def main() -> None:
    lsi_seq = "255535"
    law_name = "보험업법"
    target_date = date(2024, 10, 25)
    # lsRvsRsnListP.do용: 보험업법 lsId=001532, chrClsCd=010202
    law_id = "001532"
    chr_cls_cd = "010202"

    meta = LawChangeMeta(
        law_name=law_name,
        category="법령",
        change_type="시행",
        announcement_date=None,
        effective_date=target_date,
        source="manual-lsiSeq",
        detail_url=None,
        law_id=law_id,
        chr_cls_cd=chr_cls_cd,
        law_type="ls",
        lsi_seq=lsi_seq,
    )

    date_str = f"{target_date.year}. {target_date.month}. {target_date.day}."
    revision_text_from_list, display_meta = fetch_revision_reason_from_ls_rvs_rsn_list(
        law_id, chr_cls_cd, date_str
    )
    if display_meta:
        meta.law_number = display_meta.get("law_number")
        meta.amendment_date_str = display_meta.get("amendment_date_str")
        meta.amendment_type = display_meta.get("amendment_type")
    revision_html = None if revision_text_from_list else fetch_revision_html(meta)
    old_new_xml = fetch_old_new_html(meta)

    detail = parse_law_change(
        meta, revision_html, old_new_xml, revision_text_from_list=revision_text_from_list or None
    )
    details = [detail]

    out_dir = Path("output")
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"law_change_guide_lsi_{lsi_seq}.docx"
    generate_guide(details, target_date, out_file)

    print("생성 완료:", out_file.resolve())


if __name__ == "__main__":
    main()

