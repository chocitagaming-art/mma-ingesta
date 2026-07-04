"""Parsing of Claude's classification response (wrapped JSON).

In CI every classification failed with "Expecting value: line 1 column 1
(char 0)" and fell back to keywords (classified_claude=0 in all runs): news.py
ran json.loads directly on Claude's text, which arrives wrapped in markdown
fences (```json ... ```) or with prose around it, not as bare JSON.

_extract_json_object must tolerate that wrapping; total garbage must still
raise ValueError so the classifier keeps degrading to the keyword fallback.
No network: the Anthropic client is a stub.
"""

from types import SimpleNamespace

import pytest

from src.scrapers.news import FeedArticle, NewsClassifier, _extract_json_object


PAYLOAD = {"fighters": ["Jon Jones"], "category": "interview"}


def test_extract_json_pure():
    assert _extract_json_object('{"fighters": ["Jon Jones"], "category": "interview"}') == PAYLOAD


def test_extract_json_with_markdown_fences():
    text = '```json\n{"fighters": ["Jon Jones"], "category": "interview"}\n```'
    assert _extract_json_object(text) == PAYLOAD


def test_extract_json_with_surrounding_prose():
    text = (
        "Here is the classification you asked for:\n"
        '{"fighters": ["Jon Jones"], "category": "interview"}\n'
        "Let me know if you need anything else."
    )
    assert _extract_json_object(text) == PAYLOAD


@pytest.mark.parametrize(
    "garbage",
    ["", "I could not classify this article.", "```json\nnot json\n```", "{broken"],
    ids=["empty", "prose-only", "fenced-garbage", "unclosed-brace"],
)
def test_extract_json_garbage_raises_value_error(garbage):
    with pytest.raises(ValueError):
        _extract_json_object(garbage)


def _article(title, summary=None):
    return FeedArticle(
        source="ESPN Deportes",
        title=title,
        summary=summary,
        url="https://espndeportes.espn.com/mma/nota/_/id/1",
        published_at=None,
    )


def _classifier_answering(text):
    """NewsClassifier whose Anthropic client is a stub returning ``text``."""
    classifier = NewsClassifier(None)
    block = SimpleNamespace(type="text", text=text)
    classifier.client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(content=[block]))
    )
    return classifier


def test_classify_parses_fenced_claude_response():
    classifier = _classifier_answering(
        '```json\n{"fighters": ["Jon Jones"], "category": "interview"}\n```'
    )
    result = classifier.classify(_article("Jon Jones habla de su futuro"), fighters=[])
    assert classifier.used_claude is True
    assert result.category == "interview"
    assert result.fighters == ["Jon Jones"]


def test_classify_garbage_falls_back_to_keywords():
    classifier = _classifier_answering("no json here at all")
    result = classifier.classify(_article("Makhachev vs Della Maddalena booked"), fighters=[])
    assert classifier.used_claude is False
    assert result.category == "fight_announcement"  # keyword fallback, same as before
