"""Binary and multiclass classification reports (docs/specs/evaluation-judge.md section 1.2)."""

from collections import Counter
from collections.abc import Hashable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass


@dataclass(frozen=True)
class BinaryReport:
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int
    precision: float
    recall: float
    f1: float
    accuracy: float


@dataclass(frozen=True)
class ClassScore:
    precision: float
    recall: float
    f1: float
    support: int


@dataclass(frozen=True)
class MulticlassReport:
    total: int
    correct: int
    accuracy: float
    macro_f1: float
    per_class: Mapping[str, ClassScore]
    confusion: Mapping[tuple[str, str], int]


def _precision_recall_f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    if tp + fp == 0:
        precision = 1.0 if fn == 0 else 0.0
    else:
        precision = tp / (tp + fp)
    recall = 1.0 if tp + fn == 0 else tp / (tp + fn)
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def binary_report[T: Hashable](
    predicted: AbstractSet[T], actual: AbstractSet[T], universe: AbstractSet[T]
) -> BinaryReport:
    if not universe:
        raise ValueError("empty universe")
    if not predicted <= universe or not actual <= universe:
        raise ValueError("predicted and actual must be subsets of the universe")
    tp = len(predicted & actual)
    fp = len(predicted - actual)
    fn = len(actual - predicted)
    tn = len(universe) - tp - fp - fn
    precision, recall, f1 = _precision_recall_f1(tp, fp, fn)
    return BinaryReport(
        true_positive=tp,
        false_positive=fp,
        false_negative=fn,
        true_negative=tn,
        precision=precision,
        recall=recall,
        f1=f1,
        accuracy=(tp + tn) / len(universe),
    )


def multiclass_report(pairs: Sequence[tuple[str, str]]) -> MulticlassReport:
    """`pairs` are (expected, predicted)."""
    if not pairs:
        raise ValueError("no pairs to score")
    confusion = Counter(pairs)
    labels = sorted({label for pair in pairs for label in pair})
    per_class: dict[str, ClassScore] = {}
    for label in labels:
        tp = confusion[(label, label)]
        support = sum(1 for expected, _ in pairs if expected == label)
        predicted = sum(1 for _, guess in pairs if guess == label)
        precision, recall, f1 = _precision_recall_f1(tp, predicted - tp, support - tp)
        per_class[label] = ClassScore(precision=precision, recall=recall, f1=f1, support=support)
    correct = sum(1 for expected, guess in pairs if expected == guess)
    return MulticlassReport(
        total=len(pairs),
        correct=correct,
        accuracy=correct / len(pairs),
        macro_f1=sum(score.f1 for score in per_class.values()) / len(per_class),
        per_class=per_class,
        confusion=dict(confusion),
    )
