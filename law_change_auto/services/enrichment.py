"""Source-bound LLM drafts. Transport evidence is mandatory, never legal approval."""
import hashlib
import json
from dataclasses import asdict

from . import gemini_client as llm
from .diagnostics import warn

PROMPT_VERSION = 2
MAX_DRAFT_INPUT = 48000


def digest(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(value.encode()).hexdigest()


def source_payload(detail):
    return {'meta': asdict(detail.meta), 'reasons': detail.reason_sections,
            'main': detail.main_change_sections, 'combined': detail.combined_reason_and_main_sections,
            'comparisons': [asdict(r) for r in detail.article_comparisons],
            'opinion_deadline': detail.opinion_deadline, 'source_limit': detail.source_limit}


def source_limit(detail):
    if detail.source_limit:
        return detail.source_limit
    if '타법' in (detail.meta.amendment_type or ''):
        return '타법개정의 개정이유에는 다른 법령 내용이 포함될 수 있어 실제 변경조문 확인이 필요함.'
    if not (detail.reason_sections or detail.main_change_sections or detail.combined_reason_and_main_sections):
        return '개정이유·주요내용 원문을 확보하지 못하여 영향 판단을 보류함.'
    if detail.publication_status == '별도 확인':
        return '달력 이벤트의 종류·유효기간과 실제 개정 원문 확인이 필요함.'
    return None


def mark_source_conflicts(details):
    """Identical explanations for unrelated laws need source review, not auto drafting."""
    import re
    groups = {}
    for d in details:
        body = '\n'.join(d.reason_sections + d.main_change_sections + d.combined_reason_and_main_sections).strip()
        if len(body) >= 30:
            groups.setdefault(digest(body), []).append(d)
    for ds in groups.values():
        families = {re.sub(r'\s+', '', re.sub(r'\s*(시행령|시행규칙)$', '', d.meta.law_name)) for d in ds}
        if len(families) > 1:
            for d in ds:
                d.source_limit = d.source_limit or '서로 다른 법령에 동일한 개정이유·주요내용이 제공되어 원문 충돌 확인이 필요함.'


def build_draft_prompt(detail, *, impact=False):
    instruction = '''생명보험회사 임직원 대상 법무팀 안내서 초안을 작성함. 제공된 원문만 근거로 함.
의무 주체와 적용대상·예외·시행일을 먼저 확인하고 회사의 실제 해당 여부가 미확인이면 조건부로 씀.
공보험과 민영보험, 국민연금과 사적 연금보험, 국가기관 간 자료제공과 회사의 의무를 혼동하지 않음.
예고안은 확정 규범으로 쓰지 않고 향후 시행일을 현재 의무로 쓰지 않음.
보험계약·보험금 지급심사·약관·시스템 변경은 이를 뒷받침하는 변경조문이 있을 때만 언급함.
직접 영향 근거가 없으면 직접 영향이 확인되지 않았다고 쓰며 범용 점검 문구로 채우지 않음.
체언 종결(~음/~임), 파급효과 2~4문장. 아래 JSON 객체만 출력함.
'''
    schema = '{"summary":"핵심 개정내용 120자 이내", "impact":"회사 적용 조건과 구체적 영향 또는 확인 한계 300자 이내"}' if impact else '{"summary":"핵심 개정내용 120자 이내"}'
    if detail.selection:
        instruction += ('\n선별 기록은 우선 검토 사유이며 법률 판단의 확정이 아님. 요약과 파급효과는 선별된 회사 역할·변경 쟁점에 초점을 맞추되 아래 전체 원문의 적용조건·예외를 함께 확인함.\n'
                        + json.dumps(detail.selection, ensure_ascii=False) + '\n')
    return instruction + schema + '\n[출처·날짜·개정이유·주요내용·신구조문 원문]\n' + json.dumps(source_payload(detail), ensure_ascii=False, default=str)


def valid_evidence(detail):
    e = detail.impact_evidence
    valid = bool(e.get('status') == 'api_generated' and e.get('response_id') and e.get('provider')
                and e.get('model') and e.get('usage') and e.get('prompt_sha256')
                and e.get('prompt_sha256') == digest(e.get('prompt', ''))
                and e.get('source_sha256') == digest(source_payload(detail))
                and e.get('impact_sha256') == digest(detail.impact_analysis or '')
                and e.get('response_sha256') == digest(e.get('raw_response', '')))
    if not valid:
        return False
    try:
        raw = e['raw_response'].strip().removeprefix('```json').removesuffix('```').strip()
        original = json.loads(raw)['impact'].strip()
    except (ValueError, KeyError, TypeError, AttributeError):
        return False
    if original == detail.impact_analysis:
        return True
    correction = detail.impact_correction
    return bool(correction.get('reviewer') and correction.get('reason')
                and correction.get('original_sha256') == digest(original)
                and correction.get('final_sha256') == digest(detail.impact_analysis or '')
                and correction.get('source_sha256') == e['source_sha256'])


def valid_summary_evidence(detail):
    """Summary-only publication also requires an actual source-bound API response."""
    e = detail.impact_evidence
    if not (e.get('status') == 'api_generated' and e.get('response_id') and e.get('provider')
            and e.get('model') and e.get('usage')
            and e.get('source_sha256') == digest(source_payload(detail))
            and e.get('prompt_sha256') == digest(e.get('prompt', ''))
            and e.get('response_sha256') == digest(e.get('raw_response', ''))
            and e.get('summary_sha256') == digest(detail.summary_text or '')):
        return False
    try:
        raw = e['raw_response'].strip().removeprefix('```json').removesuffix('```').strip()
        return bool(detail.summary_text and json.loads(raw)['summary'].strip() == detail.summary_text)
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def reusable_draft(detail, *, impact=True):
    """Reusing a prior run also requires the current selection/prompt contract."""
    return bool(valid_summary_evidence(detail) and (not impact or valid_evidence(detail))
                and detail.impact_evidence.get('prompt') == build_draft_prompt(detail, impact=impact))


def set_reviewed_impact(detail, text, *, reviewer, reason):
    """Keep the API draft and bind an identified source-based correction to it."""
    if not valid_evidence(detail) or not text.strip() or not reviewer.strip() or not reason.strip():
        raise ValueError('유효한 API 초안·검토자·수정 사유가 있어야 수정 문안을 등록할 수 있음')
    raw = detail.impact_evidence['raw_response'].strip().removeprefix('```json').removesuffix('```').strip()
    original = json.loads(raw)['impact'].strip()
    detail.impact_analysis = text.strip()
    detail.impact_correction = {'reviewer': reviewer, 'reason': reason,
                                'original_sha256': digest(original), 'final_sha256': digest(text.strip()),
                                'source_sha256': digest(source_payload(detail))}
    detail.impact_evidence['impact_sha256'] = digest(text.strip())


def impact_for_document(detail):
    limit = source_limit(detail)
    if limit:
        return f'※ {limit} 파급효과 판단을 보류함.'
    if not valid_evidence(detail):
        return '※ 파급효과의 유효한 LLM 호출 증거가 없음. 생성 완료로 처리할 수 없으며 원문 대조가 필요함.'
    return detail.impact_analysis


def enrich(detail, cache_dir, *, impact=False):
    from pathlib import Path
    cache_dir = Path(cache_dir)
    # Never inherit a manually injected or stale impact after a failed call.
    detail.impact_analysis = None
    detail.impact_evidence = {}
    detail.impact_correction = {}
    detail.summary_text = None
    limit = source_limit(detail)
    if limit:
        detail.summary_text = limit
        detail.impact_evidence = {'status': 'withheld_source_limit', 'reason': limit}
        return False
    prompt = build_draft_prompt(detail, impact=impact)
    source_hash = digest(source_payload(detail))
    prompt_hash = digest(prompt)
    path = cache_dir / (digest({'version': PROMPT_VERSION, 'prompt': prompt,
                               'models': [llm.GROQ_MODEL, llm.GEMINI_MODEL]}) + '.json')
    result = None
    try:
        cached = json.loads(path.read_text())
        e = cached.get('evidence', {})
        if (e.get('status') == 'api_generated' and e.get('source_sha256') == source_hash and e.get('prompt_sha256') == prompt_hash
                and cached.get('prompt') == prompt
                and e.get('response_id') and e.get('provider') and e.get('model') and e.get('usage')
                and e.get('response_sha256') == digest(cached['raw_response'])
                and e.get('impact_sha256') == digest(cached.get('impact', ''))
                and e.get('summary_sha256') == digest(cached['summary'])
                and cached['summary'] and (not impact or cached.get('impact'))):
            result = cached
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    hit = result is not None
    if result is None and len(prompt) <= MAX_DRAFT_INPUT:
        for fetch in (llm._fetch_from_groq, llm._fetch_from_gemini):
            e = {}
            raw = fetch(detail.meta.law_name, prompt, evidence=e)
            if not raw or not (e.get('response_id') and e.get('usage') and e.get('provider') and e.get('model')):
                continue
            try:
                candidate = json.loads(raw.strip().removeprefix('```json').removesuffix('```').strip())
                keys = ('summary', 'impact') if impact else ('summary',)
                if not all(isinstance(candidate.get(k), str) and candidate[k].strip() for k in keys):
                    continue
                result = {k: candidate[k].strip() for k in keys}
                e.update(status='api_generated', source_sha256=source_hash, prompt_sha256=prompt_hash,
                         response_sha256=digest(raw), impact_sha256=digest(result.get('impact', '')),
                         summary_sha256=digest(result['summary']))
                result.update(evidence=e, raw_response=raw, prompt=prompt)
                break
            except (ValueError, TypeError, AttributeError):
                continue
    if result:
        detail.summary_text = result['summary']
        detail.impact_analysis = result.get('impact')
        detail.impact_evidence = {**result['evidence'], 'cache_hit': hit,
                                  'raw_response': result['raw_response'], 'prompt': result['prompt']}
        if not hit:
            cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    else:
        reason = 'input_limit' if len(prompt) > MAX_DRAFT_INPUT else 'no_verified_response'
        detail.impact_evidence = {'status': 'failed', 'reason': reason,
                                  'source_sha256': source_hash, 'prompt_sha256': prompt_hash}
        warn(f'AI 초안 생성 보류: {detail.meta.law_name}; {reason}; 배포 불가')
    return hit
