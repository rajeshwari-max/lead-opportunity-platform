"""The keyword filter had no word boundaries, and that is why digests were noisy.

The cases below are the actual false positives, taken from real listing titles
on this platform's own sources. Each one was an email somebody received and
should not have.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.relevance import (
    MIN_SCORE,
    Match,
    compile_keyword,
    compile_keywords,
    like_prefilter_terms,
    rank,
    score_opportunity,
)


def score(keywords, title="", summary="", vertical="", eligibility=""):
    return score_opportunity(compile_keywords(keywords), title=title,
                             summary=summary, vertical=vertical,
                             eligibility=eligibility)


# ------------------------------------------------ the false positives, by name

@pytest.mark.parametrize("keyword,title", [
    ("ict", "Request for Proposals: District Health System Strengthening"),
    ("ict", "Call for Expressions of Interest — Conflict-Affected Communities"),
    ("ict", "Consultancy: Restricted Tender for Road Rehabilitation"),
    ("ai", "Supply and Maintenance of Laboratory Equipment"),
    ("ai", "Training of Trainers on Gender Mainstreaming"),
    ("it", "Invitation for Bids: Rural Water Supply"),
    ("it", "Endline Evaluation of a Maternal Nutrition Programme"),
])
def test_a_keyword_inside_an_unrelated_word_is_not_a_match(keyword, title):
    """`%ict%` matched District, Conflict and Restricted. Every one of these
    was a wrong email."""
    assert not score([keyword], title=title).is_match


@pytest.mark.parametrize("keyword,title", [
    ("ict", "ICT for Education: Call for Proposals"),
    ("ai", "AI for Agricultural Advisory Services"),
    ("health", "District Health System Strengthening"),
    ("water", "Rural Water Supply and Sanitation"),
])
def test_the_same_keyword_still_matches_when_it_is_really_there(keyword, title):
    """The fix must not work by matching less. These are the true positives the
    old filter also caught, and they still have to pass."""
    assert score([keyword], title=title).is_match


# ------------------------------------------------------- awkward real keywords

@pytest.mark.parametrize("keyword,title", [
    ("M&E", "M&E Consultant for a Health Programme"),
    ("C4D", "C4D Strategy Development"),
    ("R&D", "R&D Grant for Clean Cooking"),
])
def test_keywords_containing_punctuation_work(keyword, title):
    r"""These are real entries in this team's inventory. `\bm&e\b` does not
    mean what it looks like it means — `&` is not a word character — which is
    why the pattern uses explicit lookarounds instead."""
    assert score([keyword], title=title).is_match


def test_a_punctuation_keyword_still_does_not_match_inside_a_word():
    assert not score(["M&E"], title="Programme Management").is_match


def test_a_multi_word_keyword_tolerates_the_separator():
    """Someone typing a keyword is naming a concept, not a byte sequence."""
    for title in ("Health System Strengthening", "Health-System Review",
                  "health  systems assessment"):
        assert score(["health system"], title=title).is_match, title


def test_a_plural_matches_its_singular_keyword():
    assert score(["farmer"], title="Grant for Smallholder Farmers").is_match


def test_a_longer_word_starting_with_the_keyword_does_not_match():
    """The inflection allowance is one or two letters, not a prefix match."""
    assert not score(["farmer"], title="Farmerville Road Project").is_match


def test_a_keyword_full_of_regex_metacharacters_is_treated_as_literal_text():
    """Someone will eventually type `C++` or `(draft)` into the keyword box.
    It has to be matched as characters, not compiled as a pattern."""
    pat = compile_keyword("C++")
    assert pat is not None
    assert pat.search("C++ Developer Training")
    assert not pat.search("Community Programme")


def test_a_meaningless_keyword_matches_nothing_rather_than_erroring():
    assert not score(["((("], title="Anything At All").is_match


def test_an_empty_keyword_is_ignored():
    assert compile_keyword("") is None
    assert compile_keyword("   ") is None


# ----------------------------------------------------------- weighting

def test_a_title_hit_alone_is_enough():
    assert score(["health"], title="Health Systems Review").is_match


def test_a_single_summary_mention_is_not_enough():
    """The weakest possible evidence, and a large share of what made digests
    noisy: one keyword appearing once in a long paragraph."""
    got = score(["health"], title="Consultancy Services",
                summary="The consultant will liaise with health authorities.")
    assert got.score < MIN_SCORE
    assert not got.is_match


def test_two_different_keywords_in_the_summary_are_enough():
    got = score(["health", "nutrition"], title="Consultancy Services",
                summary="Covering health and nutrition outcomes.")
    assert got.is_match


def test_eligibility_boilerplate_counts_for_less_than_the_title():
    """'NGOs registered in India with three years of audited accounts' describes
    who may bid, not what the work is."""
    in_title = score(["health"], title="Health Programme Evaluation")
    in_elig = score(["health"], title="Consultancy",
                    eligibility="Open to health-sector NGOs registered locally.")
    assert in_title.score > in_elig.score


def test_repeating_a_keyword_does_not_inflate_the_score():
    """A long document repeating one word is not more relevant than a short one
    naming it in the title — rewarding repetition is how boilerplate outranks
    the actual call."""
    once = score(["health"], summary="health")
    many = score(["health"], summary="health " * 50)
    assert once.score == many.score


def test_matching_more_of_a_members_keywords_scores_higher():
    one = score(["health", "nutrition", "water"], title="Health Review")
    three = score(["health", "nutrition", "water"],
                  title="Health, Nutrition and Water Review")
    assert three.score > one.score


# --------------------------------------------------------- explaining itself

def test_a_match_says_which_keyword_pulled_the_row_in():
    """A digest someone distrusts is only fixable if they can see why a row is
    in it."""
    got = score(["health", "banana"], title="Health Systems Review")
    assert got.matched_keywords == ("health",)
    assert "health" in got.explain() and "title" in got.explain()


def test_a_non_match_explains_itself_too():
    assert "no keyword matched" in score(["banana"], title="Health").explain()


def test_the_strongest_field_is_named_first():
    got = score(["health"], title="Health Review",
                eligibility="Health NGOs only.")
    assert got.where[0] == "title"


# ------------------------------------------------------------------ ranking

class Row:
    def __init__(self, name, deadline):
        self.name = name
        self.deadline = deadline

    def __repr__(self):
        return self.name


def test_the_most_relevant_comes_first_not_the_soonest():
    """The old query ordered by deadline alone, so the best match in a digest
    could sit below where someone stops reading."""
    weak = (Row("weak-but-urgent", date(2026, 9, 1)), Match(2.0, ("a",), ("summary",)))
    strong = (Row("strong", date(2027, 1, 1)), Match(9.0, ("a", "b"), ("title",)))
    assert [r.name for r, _ in rank([weak, strong])] == ["strong", "weak-but-urgent"]


def test_deadline_breaks_a_tie():
    later = (Row("later", date(2027, 1, 1)), Match(5.0, ("a",), ("title",)))
    sooner = (Row("sooner", date(2026, 9, 1)), Match(5.0, ("a",), ("title",)))
    assert [r.name for r, _ in rank([later, sooner])] == ["sooner", "later"]


def test_an_undated_row_sorts_after_dated_ones_at_the_same_score():
    dated = (Row("dated", date(2027, 6, 1)), Match(5.0, ("a",), ("title",)))
    undated = (Row("undated", None), Match(5.0, ("a",), ("title",)))
    assert [r.name for r, _ in rank([undated, dated])] == ["dated", "undated"]


# ------------------------------------------------------------- the prefilter

def test_the_prefilter_never_drops_a_real_match():
    """SQL narrows, Python decides. The order is only safe because every
    word-boundary match is also a substring match — so this must hold for
    every keyword shape the exact matcher accepts."""
    cases = [
        (["health"], "Health Systems Review"),
        (["health system"], "Health-System Strengthening"),
        (["farmer"], "Support to Farmers"),
        (["M&E"], "M&E Consultant"),
        (["ict"], "ICT for Education"),
    ]
    for keywords, title in cases:
        assert score(keywords, title=title).is_match, title
        terms = like_prefilter_terms(keywords)
        assert any(t in title.lower() for t in terms), (
            f"the SQL prefilter would have dropped {title!r} before scoring")


def test_the_prefilter_uses_the_first_word_of_a_phrase():
    """Narrowing on the whole phrase would miss 'health-system', because the
    separator is not what was typed."""
    assert like_prefilter_terms(["health system"]) == ["health"]


def test_blank_keywords_produce_no_prefilter_terms():
    assert like_prefilter_terms(["", "  "]) == []
