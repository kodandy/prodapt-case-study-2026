"""R1-5: per-sentence citation checks in ops_ai/assistant/citations.py (pure, no database)."""

from ops_ai.assistant.citations import (
    CANNOT_ANSWER_TEXT,
    EXCERPT_CHARS,
    CitedSentence,
    SourceExcerpt,
    UnsupportedSentence,
    excerpt_for,
    ground_answer,
    relevant_evidence,
    split_sentences,
    terms,
)
from ops_ai.assistant.evidence import EvidenceItem

AGGREGATE = EvidenceItem(
    ref="aggregate:decline_code:card_declined", summary="3 declined payments with decline code card_declined"
)
PAYMENT = EvidenceItem(
    ref="payment:11111111-1111-1111-1111-111111111111",
    summary=(
        "Card payment declined: card payment declined; provider acq_a; BIN 400000; decline code card_declined"
    ),
)
INCIDENT = EvidenceItem(
    ref="incident:INC-7",
    summary="Issuer outage on BIN 400000: Issuer timeouts on BIN 400000. Rerouted BIN 400000 to acq_b.",
)
EVIDENCE = (AGGREGATE, PAYMENT, INCIDENT)


def test_terms_drop_stopwords_short_words_and_a_plural_s() -> None:
    # Arrange
    question = "Why are my payments declined by the issuer?"

    # Act
    found = terms(question)

    # Assert
    assert found == frozenset({"payment", "declined", "issuer"})


def test_relevant_evidence_keeps_items_sharing_a_question_term_in_order() -> None:
    # Arrange
    question = "declined payments"

    # Act
    relevant = relevant_evidence(EVIDENCE, question, vector_refs=())

    # Assert
    assert relevant == (AGGREGATE, PAYMENT)


def test_relevant_evidence_keeps_a_vector_leg_hit_without_any_shared_term() -> None:
    # Arrange
    question = "insufficent"

    # Act
    relevant = relevant_evidence(EVIDENCE, question, vector_refs={INCIDENT.ref})

    # Assert
    assert relevant == (INCIDENT,)


def test_relevant_evidence_is_empty_for_an_unrelated_question() -> None:
    # Arrange
    question = "what is the weather in Paris tomorrow?"

    # Act
    relevant = relevant_evidence(EVIDENCE, question, vector_refs=())

    # Assert
    assert relevant == ()


def test_split_sentences_joins_a_citation_only_fragment_to_the_sentence_before_it() -> None:
    # Arrange
    text = (
        "Most declines are card_declined. [aggregate:decline_code:card_declined]\n"
        "The BIN is 400000 [incident:INC-7]."
    )

    # Act
    sentences = split_sentences(text)

    # Assert
    assert sentences == [
        "Most declines are card_declined. [aggregate:decline_code:card_declined]",
        "The BIN is 400000 [incident:INC-7].",
    ]


def test_excerpt_for_starts_at_the_first_shared_word_and_is_verbatim() -> None:
    # Arrange
    sentence = "Traffic was rerouted to acq_b [incident:INC-7]."

    # Act
    excerpt = excerpt_for(sentence, INCIDENT.summary)

    # Assert
    assert excerpt == "Rerouted BIN 400000 to acq_b."
    assert excerpt in INCIDENT.summary


def test_excerpt_for_a_long_summary_is_cut_at_a_word_boundary_within_the_limit() -> None:
    # Arrange
    summary = "alpha " * 100

    # Act
    excerpt = excerpt_for("nothing shared [x:1]", summary)

    # Assert
    assert len(excerpt) <= EXCERPT_CHARS
    assert summary.startswith(excerpt)
    assert excerpt.endswith("alpha")


def test_ground_answer_cites_each_sentence_with_its_ref_and_a_verbatim_excerpt() -> None:
    # Arrange
    text = (
        "There are 3 declined payments with code card_declined [aggregate:decline_code:card_declined]. "
        "A similar issuer outage was rerouted to acq_b [incident:INC-7]."
    )

    # Act
    grounding = ground_answer(text, EVIDENCE)

    # Assert
    assert grounding.grounded is True
    assert grounding.unsupported == ()
    assert grounding.citations == (
        CitedSentence(
            sentence=(
                "There are 3 declined payments with code card_declined "
                "[aggregate:decline_code:card_declined]."
            ),
            sources=(
                SourceExcerpt(ref=AGGREGATE.ref, excerpt="declined payments with decline code card_declined"),
            ),
        ),
        CitedSentence(
            sentence="A similar issuer outage was rerouted to acq_b [incident:INC-7].",
            sources=(SourceExcerpt(ref=INCIDENT.ref, excerpt=INCIDENT.summary),),
        ),
    )
    assert grounding.text == "\n".join(cited.sentence for cited in grounding.citations)


def test_ground_answer_flags_and_drops_a_sentence_citing_a_ref_that_was_not_retrieved() -> None:
    # Arrange
    text = (
        "There are 3 declined payments [aggregate:decline_code:card_declined]. "
        "Merchant X lost 9000 USD [payment:99999999-9999-9999-9999-999999999999]."
    )

    # Act
    grounding = ground_answer(text, EVIDENCE)

    # Assert
    assert grounding.text == "There are 3 declined payments [aggregate:decline_code:card_declined]."
    assert grounding.unsupported == (
        UnsupportedSentence(
            sentence="Merchant X lost 9000 USD [payment:99999999-9999-9999-9999-999999999999].",
            reason="unknown_ref",
            refs=("payment:99999999-9999-9999-9999-999999999999",),
        ),
    )
    assert "9000" not in grounding.text


def test_ground_answer_drops_a_sentence_mixing_a_retrieved_and_an_unknown_ref() -> None:
    # Arrange
    text = "Declines rose [aggregate:decline_code:card_declined][payment:not-retrieved]."

    # Act
    grounding = ground_answer(text, EVIDENCE)

    # Assert
    assert (grounding.grounded, grounding.text, grounding.citations) == (False, CANNOT_ANSWER_TEXT, ())
    assert [(u.reason, u.refs) for u in grounding.unsupported] == [
        ("unknown_ref", ("aggregate:decline_code:card_declined", "payment:not-retrieved"))
    ]


def test_ground_answer_flags_an_uncited_sentence_that_quotes_no_evidence() -> None:
    # Arrange
    text = (
        "Your acquirer is probably fine. Declines are card_declined [aggregate:decline_code:card_declined]."
    )

    # Act
    grounding = ground_answer(text, EVIDENCE)

    # Assert
    assert grounding.unsupported == (
        UnsupportedSentence(sentence="Your acquirer is probably fine.", reason="no_citation", refs=()),
    )
    assert [cited.sentence for cited in grounding.citations] == [
        "Declines are card_declined [aggregate:decline_code:card_declined]."
    ]


def test_ground_answer_attributes_an_uncited_verbatim_quote_to_the_chunk_it_quotes() -> None:
    # Arrange
    text = "- Issuer outage on BIN 400000: Issuer timeouts on BIN 400000."

    # Act
    grounding = ground_answer(text, EVIDENCE)

    # Assert
    assert grounding.grounded is True
    assert [source.ref for cited in grounding.citations for source in cited.sources] == [INCIDENT.ref]
    assert grounding.citations[0].sources[0].excerpt in INCIDENT.summary


def test_ground_answer_with_no_supported_sentence_returns_the_cannot_answer_text() -> None:
    # Arrange
    text = "I think the issuer is down."

    # Act
    grounding = ground_answer(text, EVIDENCE)

    # Assert
    assert (grounding.grounded, grounding.text, grounding.citations) == (False, CANNOT_ANSWER_TEXT, ())
    assert [u.reason for u in grounding.unsupported] == ["no_citation"]
