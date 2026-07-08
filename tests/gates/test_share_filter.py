"""Tests for the share-worthiness filter (gates/share_filter.py)."""
from gates.share_filter import should_share


def test_empty_text_not_shared():
    ok, reason = should_share({"text": "", "confidence": 0.9, "topic_class": "insight"})
    assert ok is False and reason == "empty_text"


def test_low_confidence_not_shared():
    ok, reason = should_share({"text": "hello", "confidence": 0.2, "topic_class": "insight"})
    assert ok is False and "low_confidence" in reason


def test_off_whitelist_topic_not_shared():
    ok, reason = should_share({"text": "hello", "confidence": 0.9, "topic_class": "random"})
    assert ok is False and "topic_off_whitelist" in reason


def test_no_action_no_question_not_shared():
    ok, reason = should_share({"text": "just a statement.", "confidence": 0.9,
                               "topic_class": "insight"})
    assert ok is False and reason == "no_action_no_question"


def test_question_form_is_shared():
    ok, reason = should_share({"text": "should we change this?", "confidence": 0.9,
                               "topic_class": "question"})
    assert ok is True and reason == "share"


def test_proposed_action_is_shared():
    ok, reason = should_share({"text": "found a bug", "confidence": 0.9,
                               "topic_class": "proposal",
                               "proposed_action": "fix the null check"})
    assert ok is True and reason == "share"
