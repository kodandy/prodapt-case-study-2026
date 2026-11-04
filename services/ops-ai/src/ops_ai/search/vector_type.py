from collections.abc import Callable, Sequence

from sqlalchemy import ColumnElement, Float, cast, literal
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType[list[float]]):
    """pgvector `vector(n)` bound as its text form ("[1.0,2.0]") and cast server-side; no extra dependency."""

    cache_ok = True

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    def get_col_spec(self, **_kw: object) -> str:
        return f"vector({self.dimensions})"

    def bind_expression(self, bindvalue: ColumnElement[list[float]]) -> ColumnElement[list[float]]:
        return cast(bindvalue, self)

    def bind_processor(self, dialect: Dialect) -> Callable[[Sequence[float] | None], str | None]:
        dimensions = self.dimensions

        def process(value: Sequence[float] | None) -> str | None:
            if value is None:
                return None
            if len(value) != dimensions:
                raise ValueError(f"expected {dimensions} dimensions, got {len(value)}")
            return "[" + ",".join(repr(float(x)) for x in value) + "]"

        return process

    def result_processor(
        self, dialect: Dialect, coltype: object
    ) -> Callable[[str | None], list[float] | None]:
        def process(value: str | None) -> list[float] | None:
            if value is None:
                return None
            inner = value.strip()[1:-1]
            return [float(x) for x in inner.split(",")] if inner else []

        return process


def cosine_distance(
    column: InstrumentedAttribute[list[float] | None], vector: Sequence[float], dimensions: int
) -> ColumnElement[float]:
    """pgvector `<=>` (cosine distance, 0 = same direction) between a vector column and a bound vector."""
    return column.op("<=>", return_type=Float)(literal(list(vector), type_=Vector(dimensions)))
