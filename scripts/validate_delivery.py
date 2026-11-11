"""Validate before shipping. Review evidence is explicit and bound to artifact hashes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from law_change_auto.services.delivery import validate_delivery, review_template, package_delivery


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('output_dir', type=Path)
    p.add_argument('--review-template', action='store_true')
    p.add_argument('--package', type=Path)
    args = p.parse_args()
    if args.review_template:
        target = args.output_dir/'delivery_review.json'
        with target.open('x', encoding='utf-8') as f:
            json.dump(review_template(args.output_dir), f, ensure_ascii=False, indent=2)
        print(f'검토용 미완료 기록 생성: {target}')
        return 0
    if args.package:
        try:
            print(package_delivery(args.output_dir, args.package))
            return 0
        except (ValueError, FileExistsError) as exc:
            print(str(exc)); return 1
    result = validate_delivery(args.output_dir)
    (args.output_dir/'delivery_validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status']=='ready' else 1


if __name__ == '__main__':
    raise SystemExit(main())
