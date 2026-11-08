"""Explicit, configurable weighted criteria. Scores are not calibrated probabilities.

One model request supplies four ratings; these are correlated criteria, not
independent agents. Local code validates them and computes the final score.
"""
from __future__ import annotations
import math

CRITERIA = ('relevance', 'understanding', 'contribution', 'timing')
KINDS = frozenset({'group_decision', 'followup', 'group_idle'})


def enabled(settings, kind):
    return settings.group_vote_enabled and kind in KINDS


def weights(settings):
    return {key: getattr(settings, 'group_vote_weight_' + key) for key in CRITERIA}


def numeric(value):
    return type(value) in (float, int) and 0 <= value <= 1 and math.isfinite(value)


def evaluate(raw, configured, threshold):
    raw = raw if isinstance(raw, dict) else {}
    invalid = [key for key in CRITERIA if not numeric(raw.get(key))]
    total = sum(configured.values())
    if total <= 0:
        raise ValueError('vote_weights_empty')
    ballots = [{ 'key': key, 'score': float(raw[key]) if numeric(raw.get(key)) else None,
                 'weight': configured[key], 'normalized_weight': configured[key] / total }
               for key in CRITERIA]
    # Compare before display rounding, so a near miss never rounds into a pass.
    score = None if invalid else sum(row['score'] * row['weight'] for row in ballots) / total
    for row in ballots:
        row['contribution'] = None if row['score'] is None else row['score'] * row['normalized_weight']
    return {'version': 1, 'source': 'single_model_criteria', 'status': 'invalid' if invalid else 'valid',
            'ballots': ballots, 'invalid_fields': invalid, 'score': score,
            'threshold': threshold, 'passed': score is not None and score >= threshold}


def apply(answer, settings, kind):
    result = dict(answer)
    # Internal metadata must never be supplied by a model or a chat message.
    result.pop('_participation_vote', None)
    result.pop('_voting_invalid', None)
    if not enabled(settings, kind):
        return result
    vote = evaluate(result.get('votes'), weights(settings), settings.group_chat_confidence_threshold)
    result['_participation_vote'] = vote
    result['confidence'] = vote['score']
    result['participate'] = vote['passed']
    if vote['status'] != 'valid':
        result['_voting_invalid'] = True
    return result


def safe_result(value):
    """Only bounded numeric criterion metadata is retained, never model prose."""
    if not isinstance(value, dict) or value.get('source') != 'single_model_criteria':
        return None
    rows = value.get('ballots')
    if not isinstance(rows, list) or len(rows) != len(CRITERIA):
        return None
    raw, configured = {}, {}
    for key, row in zip(CRITERIA, rows):
        if not isinstance(row, dict) or row.get('key') != key:
            return None
        weight = row.get('weight')
        if type(weight) is not int or not 0 <= weight <= 100:
            return None
        configured[key] = weight
        raw[key] = row.get('score')
    if not sum(configured.values()) or not numeric(value.get('threshold')):
        return None
    return evaluate(raw, configured, value['threshold'])
