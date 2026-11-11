"""Conservative, source-bound triage. Unknown applicability stays in the review queue."""
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
import re

from ..models import ArticleComparisonRow, LawChangeDetail, LawChangeMeta
from .enrichment import digest, source_limit, source_payload

RULES_PATH = Path(__file__).resolve().parents[2] / 'data' / 'selection_rules.json'
LABELS = {'priority': '우선 안내', 'conditional': '적용 여부 확인',
          'reference': '참고', 'withheld': '원문 확인 보류'}
SELECTION_POLICY = 'company-impact-v1'


def rules_contract():
    rules = json.loads(RULES_PATH.read_text(encoding='utf-8'))
    return rules, digest(rules)


def detail_from_dict(raw):
    raw = dict(raw)
    meta = dict(raw['meta'])
    for key in ('announcement_date', 'effective_date', 'calendar_event_date'):
        if meta.get(key):
            meta[key] = date.fromisoformat(meta[key])
    raw['meta'] = LawChangeMeta(**meta)
    raw['article_comparisons'] = [ArticleComparisonRow(**r) for r in raw.get('article_comparisons', [])]
    return LawChangeDetail(**raw)


def candidate_id(detail):
    # Dates and version identifiers distinguish repeated amendments of one law.
    return digest(asdict(detail.meta))


def source_units(detail, include_comparisons=False):
    """Exact excerpts only; generated summaries and impacts are never selection evidence."""
    units = []
    for field in ('main_change_sections', 'combined_reason_and_main_sections', 'reason_sections'):
        for i, text in enumerate(getattr(detail, field)):
            for part in re.split(r'\n\s*\n', text):
                if part.strip():
                    units.append({'path': f'{field}[{i}]', 'quote': part.strip()})
    if include_comparisons:
        for i, row in enumerate(detail.article_comparisons):
            units.extend(_comparison_evidence(i, row))
    return units


def _comparison_evidence(index, row):
    return [{'path': f'article_comparisons[{index}].{side}', 'quote': getattr(row, side)}
            for side in ('old_text', 'new_text') if getattr(row, side)]


def _changed_comparisons(detail):
    """Keep both sides and article context; a missing side is never proof of repeal."""
    changed = []
    article = None
    for i, row in enumerate(detail.article_comparisons):
        old, new = row.old_text or '', row.new_text or ''
        headings = {m.group() for t in (old, new)
                    for m in [re.match(r'제\d+조(?:의\d+)?', t.strip())] if m}
        if headings:
            article = next(iter(headings)) if len(headings) == 1 else None
        if re.sub(r'\s+', '', old) == re.sub(r'\s+', '', new):
            continue
        if (re.sub(r'[^가-힣]', '', old) == '생략'
                and re.sub(r'[^가-힣]', '', new) == '현행과같음'):
            continue
        changed.append((i, row, article))
    return changed


def _sentences(units):
    # Keep literal substrings for source-hash/evidence validation. Do not let a
    # different sentence's subject or exception establish the company's duty.
    for unit in units:
        for part in re.split(r'(?<=[.!?])\s+(?=[^\d])', unit['quote']):
            if part.strip():
                yield {'path': unit['path'], 'quote': part.strip()}


def _unqualified_change(text, match):
    if re.search(r'해당하지|적용하지|제외|자동차\s*보험|손해보험회사|공공기관.{0,15}한정|우선지원\s*대상기업', text):
        return False
    if re.search(r'(하지\s*(않|아니)|없|아니)', text[match.end():match.end()+15]):
        return False
    # A regulator/agency later in the same clause must not inherit the earlier
    # insurer subject. Ambiguous clauses stay conditional.
    return not re.search(r'(위원회|장관|공단|관리원|감독원|청장|소방서장)[은는이가]', match.group())


def _editorial_only(detail):
    """Only explicitly limited editorial source text qualifies for automatic reference."""
    units = source_units(detail)
    body = ' '.join(u['quote'] for u in units)
    return bool(body and re.search(r'(명칭|제명|인용).{0,25}(만|단순).{0,20}(정비|변경)', body)
                and re.search(r'(의무|절차|권리).{0,30}(변경.{0,5}없|종전과 같)', body)
                and not re.search(r'(제출처|보고대상|수신기관|적용대상).{0,30}(변경|확대|추가)', body))


def assess(detail):
    rules, rules_hash = rules_contract()
    base = {'policy': SELECTION_POLICY, 'rules_version': rules['version'],
            'rules_sha256': rules_hash, 'source_sha256': digest(source_payload(detail)),
            'candidate_id': candidate_id(detail), 'method': 'rules', 'review': None}
    units = source_units(detail)
    def decision(status, rule_id, reason, role='', change='', evidence=None):
        return {**base, 'status': status, 'label': LABELS[status], 'rule_id': rule_id,
                'reason': reason, 'role': role, 'change': change, 'evidence': evidence or []}

    limit = source_limit(detail)
    placeholder = units and all(re.search(r'자동으로 추출하지 못|원문을 직접 확인|원문 미확보', u['quote']) for u in units)
    if limit or not units or placeholder:
        return decision('withheld', 'source_limit', limit or '판단에 사용할 개정이유·주요내용 원문이 없음.')
    name = re.sub(r'\s+', '', detail.meta.law_name)
    for rule in rules['priority_rules']:
        if not re.search(rule['law_pattern'], name):
            continue
        for unit in _sentences(units):
            text = re.sub(r'\s+', ' ', unit['quote'])
            match = re.search(rule['pattern'], text)
            if match and _unqualified_change(text, match):
                return decision('priority', rule['id'], '원문에서 회사 역할과 실질적 변경을 함께 확인한 우선 검토 후보임.',
                                rule['role'], rule['change'], [unit])

    changed = _changed_comparisons(detail)
    comparison_units = [e for i, row, _ in changed for e in _comparison_evidence(i, row)]
    for rule in rules.get('comparison_priority_rules', []):
        if not re.search(rule['law_pattern'], name):
            continue
        for i, row, article in changed:
            if (article in rule['articles'] and re.fullmatch(r'<삭\s*제>', (row.new_text or '').strip())
                    and re.search(rule['old_pattern'], re.sub(r'\s+', ' ', row.old_text or ''))):
                return {**decision('priority', rule['id'], rule['reason'], rule['role'], rule['change'],
                                   _comparison_evidence(i, row)), 'scope_sources': rule['scope_sources']}

    business_changes = [u for u in comparison_units if re.search(rules['business_scope_pattern'], u['quote'])]
    if _editorial_only(detail) and not business_changes:
        return decision('reference', 'explicit_editorial', '원문이 명칭·인용만의 정비이고 권리·의무·절차 변화가 없음을 명시함.', evidence=units)
    body = ' '.join(u['quote'] for u in units)
    if ('국민연금' in name and re.search(r'가입기간.{0,30}산입|산입.{0,30}기간', body)
            and not re.search(r'사용자|사업주|신고|원천징수|보수총액', body) and not business_changes):
        return decision('reference', 'individual_pension', '개인 가입기간·수급에 관한 원문으로 회사의 신고·보험료 처리 변화가 확인되지 않음.',
                        '개인 연금 가입자', evidence=units)
    for rule in rules.get('reference_rules', []):
        if (re.search(rule['law_pattern'], name) and re.search(rule['pattern'], body)
                and changed and all(article in rule['articles'] for _, _, article in changed)
                and not business_changes and not re.search(rules['business_scope_pattern'], body)):
            return decision('reference', rule['id'], rule['reason'], rule['role'], evidence=units+comparison_units)
    for rule in rules['conditional_rules']:
        if not re.search(rule.get('law_pattern', '.*'), name):
            continue
        for unit in units + comparison_units:
            if re.search(rule['pattern'], unit['quote']):
                return decision('conditional', rule['id'], rule['reason'], rule['role'], evidence=[unit])
    if business_changes:
        return decision('conditional', 'changed_company_scope', '신구조문에 회사 역할과 관련된 변경이 있어 적용 범위를 확인해야 함. 빈 신조문만으로 삭제를 확정하지 않음.',
                        '변경 조문의 적용 주체', evidence=business_changes)
    return decision('conditional', 'unresolved_scope', '회사 적용 주체와 실질적 업무 변화를 자동으로 연결하지 못함. 검토 후 분류해야 함.', evidence=units[:1])


def apply_review(detail, review):
    automatic = assess(detail)
    required = ('candidate_id', 'source_sha256', 'status', 'reviewer', 'reason', 'role', 'change')
    if any(not isinstance(review.get(k), str) or not review[k].strip() for k in required):
        raise ValueError('선별 검토에는 후보·원문 해시, 분류, 실제 검토자, 사유, 역할, 변경내용이 필요함')
    if review['candidate_id'] != candidate_id(detail) or review['source_sha256'] != automatic['source_sha256']:
        raise ValueError('선별 검토 대상의 버전 또는 원문이 변경됨')
    if review['status'] not in LABELS:
        raise ValueError('알 수 없는 선별 분류')
    if automatic['status'] == 'withheld' and review['status'] != 'withheld':
        raise ValueError('원문 한계를 해결한 새 원문으로 다시 평가해야 함')
    units = source_units(detail, include_comparisons=True)
    evidence = review.get('evidence', [])
    if review['status'] != 'withheld' and (not evidence or any(
            not any(e.get('path') == u['path'] and e.get('quote') and e['quote'] in u['quote'] for u in units)
            for e in evidence)):
        raise ValueError('선별 검토에 실제 원문의 위치·인용문이 필요함')
    return {**automatic, 'method': 'reviewed', 'review': review, 'rule_id': 'reviewed',
            **{k: review[k] for k in ('status', 'reason', 'role', 'change')},
            'label': LABELS[review['status']], 'evidence': evidence}


def select_candidates(details, review_path=None):
    reviews = {}
    if review_path:
        payload = json.loads(Path(review_path).read_text(encoding='utf-8'))
        if payload.get('schema') != 1:
            raise ValueError('선별 검토 파일 스키마 불일치')
        for entry in payload['decisions']:
            key = entry['candidate_id']
            if key in reviews:
                raise ValueError('동일 후보의 선별 검토 중복')
            reviews[key] = entry
    ids = {candidate_id(d) for d in details}
    if set(reviews) - ids:
        raise ValueError('이번 실행에 없는 후보의 선별 검토가 포함됨')
    for d in details:
        d.selection = apply_review(d, reviews[candidate_id(d)]) if candidate_id(d) in reviews else assess(d)
    return [d for d in details if d.selection['status'] == 'priority']


def validate_assessment(detail):
    actual = detail.selection
    expected = apply_review(detail, actual['review']) if actual.get('method') == 'reviewed' else assess(detail)
    if actual != expected:
        raise ValueError('원문·규칙·검토 기록과 선별 결과 불일치')


def write_registry(base, details, file_map):
    base = Path(base)
    payload = {'schema': 1, 'policy': SELECTION_POLICY, 'rules_sha256': rules_contract()[1],
               'counts': {status: sum(d.selection['status'] == status for d in details) for status in LABELS},
               'candidates': [{'no': i, 'law_name': d.meta.law_name, 'publication_status': d.publication_status,
                               'output_file': file_map[id(d)].name if id(d) in file_map else None,
                               'assessment': d.selection} for i, d in enumerate(details, 1)]}
    (base/'candidate_details.json').write_text(json.dumps([asdict(d) for d in details], ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    (base/'candidate_selection.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 후보 선별 검토 목록', '', '자동 분류는 법률 판단의 확정이나 배포 승인이 아님. 원문 기준 회사 적용성과 제외 사유를 검토함.', '']
    for row in payload['candidates']:
        a = row['assessment']
        lines += [f"## {row['no']}. {row['law_name']} — {a['label']}", '',
                  f"- 사유: {a['reason']}", f"- 회사 역할: {a['role'] or '미확인'}",
                  f"- 실질적 변경: {a['change'] or '확인 필요'}", f"- 상태: {row['publication_status'] or '미분류'}",
                  f"- 개별본: {row['output_file'] or '미발행'}", f"- 후보 ID: `{a['candidate_id']}`", '']
        for e in a['evidence']:
            lines += [f"근거 `{e['path']}`:", '', '> ' + e['quote'].replace('\n', '\n> '), '']
    (base/'후보선별_검토목록.md').write_text('\n'.join(lines), encoding='utf-8')
    return payload
