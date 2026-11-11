"""Public local observations, with no account registry, files, or network."""
import pytest

from azusa_bot.local_conversation import LocalConversation


SELF, HUMAN, OTHER = '10001', '20001', '30001'


def message(number, text='', user=HUMAN, **fields):
    return dict(message_id=number, user_id=user, text=text, **fields)


def observe(scene, number, text='', user=HUMAN, **fields):
    return scene.observe('room', message(number, text, user, **fields), self_id=SELF, names=['小梓'])


def assessment(number, *, action='reply', ids=None, evidence=None, anchor=None, **fields):
    return dict(action=action, addressee_ids=[SELF] if ids is None else ids,
                response_to_message_id=number, evidence_message_ids=evidence or [number],
                thread_anchor=anchor or number, reason='local_evidence', **fields)


@pytest.fixture
def scene():
    return LocalConversation()


def test_explicit_self_vocative_fast_reply_without_model(scene):
    row = observe(scene, 'one', '小梓，你怎么看')
    assert row['target'] == 'self' and not row['needs_assessment']
    plan = scene.resolve_fast('room', 'one')
    assert plan['action'] == 'reply' and plan['addressee_ids'] == [SELF]
    assert scene.valid(plan)
    assert scene.resolve_fast('room', 'one') == plan


def test_bare_native_call_has_facts_separate_from_inference(scene):
    row = observe(scene, 'one', mentioned_user_ids=[SELF], mention_metadata_available=True)
    assert row['facts']['mentioned_user_ids'] == [SELF]
    assert row['source'] == 'native_call'
    assert scene.resolve_fast('room', 'one')['action'] == 'reply'


def test_sole_native_self_call_with_normal_prose_has_fast_reply(scene):
    row = observe(scene, 'one', '今天过得怎么样', mentioned_user_ids=[SELF], mention_metadata_available=True)
    assert row['target'] == 'self' and not row['needs_assessment']
    assert scene.resolve_fast('room', 'one')['action'] == 'reply'


def test_legacy_platform_self_call_does_not_fabricate_native_ids(scene):
    row = observe(scene, 'one', '帮我看看', mentioned=True, other_mentioned=False,
                  mention_metadata_available=False)
    assert not row['needs_assessment'] and row['source'] == 'legacy_self_call'
    assert row['facts']['platform_mentioned'] and not row['facts']['mention_metadata_available']
    assert row['facts']['mentioned_user_ids'] == []
    assert scene.resolve_fast('room', 'one')['action'] == 'reply'


def test_legacy_platform_call_with_other_recipient_or_foreign_quote_needs_assessment(scene):
    row = observe(scene, 'one', '看看这个', mentioned=True, other_mentioned=True,
                  mention_metadata_available=False)
    assert row['needs_assessment']
    row = observe(scene, 'two', '怎么看', mentioned=True, reply_to_id='quote',
                  quoted_message=message('quote', '别人说的话', user=OTHER), mention_metadata_available=False)
    assert row['needs_assessment']


def test_authoritative_empty_native_list_does_not_use_legacy_flag(scene):
    row = observe(scene, 'one', '你怎么看', mentioned=True, mentioned_user_ids=[], mention_metadata_available=True)
    assert row['needs_assessment'] and row['target'] == 'unknown'


def test_native_self_call_with_foreign_vocative_or_reported_speech_needs_assessment(scene):
    observe(scene, 'intro', '你好', user=OTHER, display_name='小李')
    row = observe(scene, 'one', '小李，你看刚才那条消息', mentioned_user_ids=[SELF], mention_metadata_available=True)
    assert row['needs_assessment']


def test_native_self_plus_everyone_does_not_take_self_shortcut(scene):
    row = observe(scene, 'one', mentioned_user_ids=[SELF], mention_metadata_available=True, mentions_everyone=True)
    assert row['target'] == 'group' and row['needs_assessment']
    row = observe(scene, 'two', '刚才又把那个人认错了', mentioned_user_ids=[SELF], mention_metadata_available=True)
    assert row['needs_assessment']


def test_native_mention_in_prose_and_quote_are_not_automatic_addressees(scene):
    row = observe(scene, 'one', '刚才@10001又把@30001认错了',
                  mentioned_user_ids=[SELF, OTHER], mention_metadata_available=True)
    assert row['needs_assessment'] and row['target'] == 'unknown'
    assert scene.resolve_fast('room', 'one') is None
    row = observe(scene, 'two', '你看这个人', reply_to_id='quoted', quoted_message={
        'message_id': 'quoted', 'user_id': OTHER, 'text': '此前的一句话'})
    assert row['needs_assessment'] and row['facts']['quoted_author_id'] == OTHER
    assert row['target'] == 'unknown'


def test_current_quote_self_and_mention_human_can_be_assessed_as_self(scene):
    observe(scene, 'one', '你说的是@30001吗', reply_to_id='quote',
            quoted_message=message('quote', '之前的话', user=SELF),
            mentioned_user_ids=[OTHER], mention_metadata_available=True)
    plan = scene.accept('room', 'one', assessment('one', evidence=['one', 'quote'], anchor='quote'))
    assert plan and plan['target'] == 'self'


def test_local_ack_resolves_quote_author_without_fabricating_quote_body(scene):
    observe(scene, 'one', '小梓，你好')
    scene.resolve_fast('room', 'one')
    observe(scene, 'ack', '之前确实说过的话', user=SELF, role='assistant', response_to_message_id='one')
    row = observe(scene, 'two', '这句话是什么意思', reply_to_id='ack')
    assert row['facts']['quoted_author_id'] == SELF
    assert row['facts']['quoted_author_source'] == 'local_observation'
    assert row['target'] == 'self' and row['needs_assessment']
    assert 'quoted_message' not in row and 'text' not in row['facts']
    plan = scene.accept('room', 'two', assessment('two', evidence=['one', 'ack', 'two'], anchor='one'))
    assert plan and scene.valid(plan)


def test_quote_relation_keeps_visible_quote_anchor_and_does_not_use_legacy_fast_call(scene):
    observe(scene, 'one', '小梓，你好')
    scene.resolve_fast('room', 'one')
    observe(scene, 'ack', '你好', user=SELF, role='assistant', response_to_message_id='one')
    row = observe(scene, 'two', '接着说', reply_to_id='ack', mentioned=True, mention_metadata_available=False)
    assert row['thread_anchor'] == 'ack' and set(row['evidence_message_ids']) == {'ack', 'two'}
    assert row['source'] == 'quoted_self_candidate' and row['needs_assessment']
    plan = scene.accept('room', 'two', assessment('two', evidence=row['evidence_message_ids'], anchor=row['thread_anchor']))
    assert plan and scene.valid(plan)


def test_recent_real_member_quote_does_not_become_a_self_quote(scene):
    observe(scene, 'one', '别人说的话', user=OTHER)
    row = observe(scene, 'two', '这句话是什么意思', reply_to_id='one')
    assert row['facts']['quoted_author_id'] == OTHER
    assert row['target'] == 'unknown' and row['needs_assessment']


def test_public_member_name_is_local_evidence_and_third_person_mention_is_not_vocative(scene):
    observe(scene, 'intro', '你好', user=OTHER, display_name='灰泽满')
    row = observe(scene, 'call', '灰泽满，你几点回来')
    assert row['target'] == 'others' and row['addressee_ids'] == [OTHER]
    assert scene.resolve_fast('room', 'call')['action'] == 'observe'
    row = observe(scene, 'third', '灰泽满今天直播了，你觉得呢')
    assert row['source'] != 'public_vocative'


def test_native_mention_label_is_learned_without_alias_configuration(scene):
    observe(scene, 'one', '看这位', mentions=[dict(user_id=OTHER, display_name='灰泽满')],
            mention_metadata_available=True)
    row = observe(scene, 'two', '灰泽满你怎么看')
    assert row['addressee_ids'] == [OTHER] and row['target'] == 'others'


def test_shared_public_name_stays_ambiguous(scene):
    observe(scene, 'intro', '你好', user=OTHER, display_name='小梓')
    row = observe(scene, 'one', '小梓，你好')
    assert row['needs_assessment'] and row['source'] == 'ambiguous_name'
    assert scene.accept('room', 'one', assessment('one')) is None


def test_human_turn_to_another_human_replaces_local_target(scene):
    observe(scene, 'intro', '你好', user=OTHER, display_name='小李')
    observe(scene, 'one', '小梓，你好')
    old = scene.resolve_fast('room', 'one')
    observe(scene, 'two', '小李，你到家了吗')
    assert not scene.valid(old)
    row = observe(scene, 'three', '你怎么这么慢')
    assert row['target'] == 'others' and row['addressee_ids'] == [OTHER]
    assert not row['needs_assessment']
    assert scene.resolve_fast('room', 'three')['action'] == 'observe'


def test_unknown_second_person_cannot_be_claimed_by_model_without_self_evidence(scene):
    observe(scene, 'one', '你到底怎么想的')
    assert scene.accept('room', 'one', assessment('one')) is None
    assert scene.accept('room', 'one', assessment('one', ids=[], target='group')) is None
    wait = scene.accept('room', 'one', assessment('one', action='wait', ids=[], target='unknown'))
    assert wait and wait['action'] == 'wait'


def test_local_followup_needs_semantic_assessment_but_accepts_real_self_evidence(scene):
    observe(scene, 'one', '小梓，你好')
    scene.resolve_fast('room', 'one')
    observe(scene, 'ack', '你好呀', user=SELF, role='assistant', response_to_message_id='one')
    row = observe(scene, 'two', '你今天怎么样')
    assert row['target'] == 'self' and row['needs_assessment']
    plan = scene.accept('room', 'two', assessment('two', anchor='one', evidence=['one', 'ack', 'two']))
    assert plan and scene.valid(plan)


def test_another_humans_self_dialogue_is_not_my_self_evidence(scene):
    observe(scene, 'one', '小梓，你好', user=OTHER)
    observe(scene, 'two', '你怎么这样')
    assert scene.accept('room', 'two', assessment('two', evidence=['one', 'two'])) is None


def test_explicit_semantic_response_to_own_public_reply_can_handoff_to_another_human(scene):
    observe(scene, 'one', '小梓，你好')
    scene.resolve_fast('room', 'one')
    observe(scene, 'ack', '大家都可以一起讨论这个问题', user=SELF, role='assistant', response_to_message_id='one')
    observe(scene, 'two', '你刚才那句话是什么意思', user=OTHER)
    plan = scene.accept('room', 'two', assessment('two', anchor='ack', evidence=['ack', 'two']) |
                        {'response_to_message_id': 'ack'})
    assert plan and scene.valid(plan)
    assert plan['thread_anchor'] == 'one' and plan['visible_thread_anchor'] == 'ack'
    assert plan['evidence_message_ids'] == ['ack', 'two']


def test_arbitrary_self_public_row_does_not_automatically_claim_new_human_you(scene):
    observe(scene, 'one', '小梓，你好')
    observe(scene, 'ack', '公开回复', user=SELF, role='assistant', response_to_message_id='one')
    observe(scene, 'two', '你怎么这样', user=OTHER)
    assert scene.accept('room', 'two', assessment('two', anchor='ack', evidence=['ack', 'two'])) is None


def test_begin_assessment_sees_ack_received_while_question_waited(scene):
    observe(scene, 'one', '小梓，你好')
    scene.resolve_fast('room', 'one')
    observe(scene, 'two', '你说的是什么意思', user=OTHER)
    observe(scene, 'ack', '现在确认送达的公开回复', user=SELF, role='assistant', response_to_message_id='one')
    value = assessment('two', anchor='ack', evidence=['ack', 'two']) | {'response_to_message_id': 'ack'}
    assert scene.accept('room', 'two', value) is None
    assert 'ack' in scene.begin_assessment('room', 'two')['permitted_evidence_message_ids']
    plan = scene.accept('room', 'two', value)
    assert plan and scene.valid(plan)


def test_begin_assessment_cannot_revive_real_human_turn_away_even_after_late_ack(scene):
    observe(scene, 'intro', '你好', user=OTHER, display_name='小李')
    observe(scene, 'one', '小梓，你好')
    scene.resolve_fast('room', 'one')
    observe(scene, 'two', '你怎么看')
    observe(scene, 'three', '小李，你来回答吧')
    observe(scene, 'ack', '晚到的送达回执', user=SELF, role='assistant', response_to_message_id='one', relation_active=False)
    assert scene.begin_assessment('room', 'two') is None
    assert scene.accept('room', 'two', assessment('two', anchor='one', evidence=['one', 'two'])) is None


def test_sliding_prompt_evidence_does_not_require_hidden_canonical_thread_anchor(scene):
    observe(scene, 'old', '小梓，先举个例子')
    scene.resolve_fast('room', 'old')
    observe(scene, 'ack', '先代入一', user=SELF, role='assistant', response_to_message_id='old')
    observe(scene, 'recent', '你继续')
    assert scene.accept('room', 'recent', assessment('recent', anchor='old', evidence=['old', 'ack', 'recent']))
    observe(scene, 'recent-ack', '再代入二', user=SELF, role='assistant', response_to_message_id='recent')
    observe(scene, 'latest', '你接着讲')
    visible = scene.begin_assessment('room', 'latest', message_ids=['recent', 'recent-ack', 'latest', 'unknown'])
    assert visible['permitted_evidence_message_ids'] == ['recent', 'recent-ack', 'latest']
    assert 'old' not in visible['evidence_message_ids'] and visible['thread_anchor'] != 'old'
    assert scene.accept('room', 'latest', assessment('latest', anchor='old', evidence=['old', 'latest'])) is None
    plan = scene.accept('room', 'latest', assessment('latest', anchor='recent', evidence=['recent', 'recent-ack', 'latest']))
    assert plan and scene.valid(plan)
    assert plan['thread_anchor'] == 'old' and plan['visible_thread_anchor'] == 'recent'
    assert plan['evidence_message_ids'] == ['recent', 'recent-ack', 'latest']
    observe(scene, 'correction', '我补充一下，用三来举例')
    assert not scene.valid(plan)


@pytest.mark.parametrize('wording', ['我补充一下，用三来举例', '刚才说错了，用三', '改成三', '等等，是三'])
def test_explicit_revision_advances_accepted_same_user_thread_only(scene, wording):
    observe(scene, 'start', '小梓，讲讲这个计算')
    scene.resolve_fast('room', 'start')
    observe(scene, 'ack', '先挑个数', user=SELF, role='assistant', response_to_message_id='start')
    row = observe(scene, 'question', '那用哪个数')
    assert row['target'] == 'unknown' and row['needs_assessment']
    pending = scene.accept('room', 'question', assessment('question', anchor='start', evidence=['start', 'ack', 'question']))
    assert pending and scene.valid(pending)
    observe(scene, 'independent', '小梓，再解释一下操作系统', user=OTHER)
    independent = scene.resolve_fast('room', 'independent')
    revision = observe(scene, 'revision', wording)
    assert revision['source'] == 'local_continuation' and revision['target'] == 'self'
    assert revision['needs_assessment'] and scene.resolve_fast('room', 'revision') is None
    assert not scene.valid(pending) and scene.valid(independent)


def test_revision_does_not_invent_recipient_without_local_binding(scene):
    row = observe(scene, 'one', '我补充一下，用三来举例')
    assert row['target'] == 'unknown' and row['needs_assessment']
    assert scene.accept('room', 'one', assessment('one')) is None


def test_nonleading_own_name_is_semantic_evidence_but_never_fast_permission(scene):
    row = observe(scene, 'one', '请回答一下，小梓')
    assert row['needs_assessment'] and row['facts']['self_name_candidate']
    assert scene.resolve_fast('room', 'one') is None
    plan = scene.accept('room', 'one', assessment('one'))
    assert plan and plan['target'] == 'self'


def test_open_statement_goes_to_assessment_and_can_get_public_reply(scene):
    row = observe(scene, 'one', '今天的天空真好看')
    assert row['needs_assessment'] and scene.resolve_fast('room', 'one') is None
    plan = scene.accept('room', 'one', assessment('one', ids=[]))
    assert plan and plan['target'] == 'group'


def test_reported_everyone_is_not_a_group_invitation(scene):
    row = observe(scene, 'one', '大家都说你很自恋')
    assert row['target'] == 'unknown'
    assert scene.accept('room', 'one', assessment('one', ids=[], target='group')) is None


@pytest.mark.parametrize('wording', ['你们两位觉得晚饭吃什么？', '你们俩怎么看', '两位觉得怎么样', '你俩聊聊'])
def test_plural_invitation_is_not_bound_as_singular_followup(scene, wording):
    observe(scene, 'one', '小梓，你好')
    scene.resolve_fast('room', 'one')
    row = observe(scene, 'two', wording)
    assert row['target'] == 'group' and row['needs_assessment']
    assert scene.accept('room', 'two', assessment('two', ids=[], target='group'))


def test_unrelated_events_and_same_user_new_explicit_question_do_not_cancel(scene):
    observe(scene, 'one', '小梓，解释一下编译器')
    first = scene.resolve_fast('room', 'one')
    observe(scene, 'unrelated', '我去吃饭了', user=OTHER)
    assert scene.valid(first)
    observe(scene, 'two', '小梓，顺便再解释一下操作系统')
    second = scene.resolve_fast('room', 'two')
    assert first['thread_id'] != second['thread_id']
    assert scene.valid(first) and scene.valid(second)


def test_related_native_reply_cancels_pending_without_cancelling_other_threads(scene):
    observe(scene, 'one', '小梓，解释编译器')
    first = scene.resolve_fast('room', 'one')
    observe(scene, 'two', '小梓，解释操作系统')
    second = scene.resolve_fast('room', 'two')
    observe(scene, 'answer', '这个我来回答', user=OTHER, reply_to_id='one')
    assert not scene.valid(first) and scene.valid(second)


def test_newer_same_thread_event_rejects_late_assessment(scene):
    observe(scene, 'one', '小梓，你好')
    observe(scene, 'two', '你今天怎么样')
    observe(scene, 'three', '你有空吗')
    assert scene.accept('room', 'two', assessment('two', anchor='one', evidence=['one', 'two'])) is None


@pytest.mark.parametrize('change', [
    dict(evidence_message_ids=['one', 'invented']),
    dict(addressee_ids=['unknown_actor']),
    dict(thread_anchor='invented'),
    dict(response_to_message_id='invented'),
    dict(reason='a long free form instruction'),
    dict(evidence_message_ids=[]),
    dict(action='send'),
    dict(target='others'),
    dict(action=['reply']),
])
def test_invalid_model_plan_fails_closed_without_mutation(scene, change):
    observe(scene, 'one', '小梓，你好')
    before = scene.get('room', 'one')
    assert scene.accept('room', 'one', assessment('one') | change) is None
    assert scene.get('room', 'one') == before


def test_later_real_event_was_not_visible_to_earlier_assessment(scene):
    observe(scene, 'one', '你怎么看')
    observe(scene, 'future', '小梓，你好')
    assert scene.accept('room', 'one', assessment('one', evidence=['one', 'future'])) is None
    assert scene.get('room', 'one')['permitted_evidence_message_ids'] == ['one']


def test_malformed_target_and_guard_fail_closed(scene):
    observe(scene, 'one', '今天天气很好')
    assert scene.accept('room', 'one', assessment('one', ids=[], target=[])) is None
    assert not scene.valid({'scope': [], 'plan_id': []})


def test_unresolved_quote_id_is_a_pointer_not_visible_text_evidence(scene):
    observe(scene, 'one', '这个什么意思', reply_to_id='missing')
    assert scene.accept('room', 'one', assessment('one', evidence=['one', 'missing'])) is None


def test_instances_and_scopes_have_no_shared_state(scene):
    observe(scene, 'one', '小梓，你好')
    plan = scene.resolve_fast('room', 'one')
    independent = LocalConversation()
    assert not independent.valid(plan)
    row = observe(independent, 'two', '你怎么看')
    assert row['target'] == 'unknown'
    row = scene.observe('other-room', message('two', '你怎么看'), self_id=SELF, names=['小梓'])
    assert row['target'] == 'unknown'


def test_duplicate_observation_and_returned_mutation_do_not_change_state(scene):
    observe(scene, 'one', '小梓，你好')
    plan = scene.resolve_fast('room', 'one')
    row = observe(scene, 'one', '不同的重传正文')
    row['addressee_ids'].append(OTHER)
    assert scene.valid(plan)
    altered = dict(plan, action='observe')
    assert not scene.valid(altered)
    assert scene.get('room', 'one')['addressee_ids'] == [SELF]


def test_new_assessment_for_same_event_revokes_old_plan(scene):
    observe(scene, 'one', '小梓，你好')
    first = scene.resolve_fast('room', 'one')
    second = scene.accept('room', 'one', assessment('one', action='observe'))
    assert second and not scene.valid(first) and scene.valid(second)


def test_inactive_ack_is_public_evidence_but_does_not_extend_binding():
    now = [10.]
    scene = LocalConversation(clock=lambda:now[0], ttl_seconds=100, followup_seconds=10)
    observe(scene, 'one', '小梓，你好')
    now[0] += 9
    observe(scene, 'ack', '你好', user=SELF, role='assistant', response_to_message_id='one', relation_active=False)
    now[0] += 2
    assert observe(scene, 'two', '你怎么看')['target'] == 'unknown'


def test_privacy_clear_invalidates_evidence_and_member_aliases(scene):
    observe(scene, 'one', '小梓，你好')
    plan = scene.resolve_fast('room', 'one')
    scene.clear(user=HUMAN)
    assert not scene.valid(plan) and scene.get('room', 'one') is None
    observe(scene, 'two', '小梓，你好')
    second = scene.resolve_fast('room', 'two')
    scene.clear('room')
    assert not scene.valid(second)


def test_capacity_and_expiry_fail_closed():
    now = [10.]
    scene = LocalConversation(clock=lambda:now[0], max_scopes=1, max_events=2, max_threads=2, ttl_seconds=20)
    observe(scene, 'one', '小梓，你好')
    plan = scene.resolve_fast('room', 'one')
    observe(scene, 'two', '今天很好', user=OTHER)
    observe(scene, 'three', '天气真好', user=OTHER)
    assert not scene.valid(plan)
    now[0] += 21
    assert scene.get('room', 'three') is None
    observe(scene, 'four', '小梓，你好')
    plan = scene.resolve_fast('room', 'four')
    scene.observe('another', message('five', '你好'), self_id=SELF)
    assert not scene.valid(plan)


def test_followup_binding_expires_before_history():
    now = [10.]
    scene = LocalConversation(clock=lambda:now[0], ttl_seconds=100, followup_seconds=10)
    observe(scene, 'intro', '你好', user=OTHER, display_name='小李')
    observe(scene, 'one', '小李，你好')
    now[0] += 11
    assert observe(scene, 'two', '你怎么看')['target'] == 'unknown'


def test_recent_public_rows_seed_names_but_never_import_decisions(scene):
    row = scene.observe('room', message('two', '灰泽满，你好'), self_id=SELF, names=['小梓'], recent=[
        message('one', '你好', user=OTHER, display_name='灰泽满', target='self', addressee_ids=[SELF])])
    assert row['target'] == 'others' and row['addressee_ids'] == [OTHER]


def test_unrequested_self_reply_does_not_capture_a_human(scene):
    observe(scene, 'one', '今天的天空真好看')
    scene.accept('room', 'one', assessment('one', ids=[], target='group'))
    observe(scene, 'ack', '是呀', user=SELF, role='assistant', response_to_message_id='one')
    assert observe(scene, 'two', '你怎么看')['target'] == 'unknown'
