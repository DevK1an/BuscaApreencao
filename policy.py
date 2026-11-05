"""Regras de preço: todos os cálculos são feitos em centavos."""
from dataclasses import dataclass
import json
from pathlib import Path
import re

MONEY = re.compile(r"(?<![\d.,-])R\$\s*((?:\d{1,3}(?:\.\d{3})+|\d+),\d{2})(?![\d.,])", re.I)


class CannotVerify(ValueError):
    """A página não forneceu informação suficiente para continuar."""


class OfferUnavailable(CannotVerify):
    """Indisponibilidade normal: monitorar novamente sem pedir intervenção."""


def parse_brl(text: str) -> int:
    text = text.replace("\xa0", " ").strip()
    matches = list(MONEY.finditer(text))
    if len(matches) != 1 or re.search(r"-\s*R\$", text):
        raise CannotVerify("Valor em reais ausente ou ambíguo.")
    return int(matches[0][1].replace(".", "").replace(",", ""))


def brl(cents: int) -> str:
    whole, fraction = divmod(cents, 100)
    return "R$ " + f"{whole:,}".replace(",", ".") + f",{fraction:02d}"


def positive_int(value, name: str, maximum: int = 999) -> int:
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValueError(f"{name} deve ser um número inteiro positivo.")
    number = int(value)
    if not 1 <= number <= maximum:
        raise ValueError(f"{name} deve ficar entre 1 e {maximum}.")
    return number


@dataclass(frozen=True)
class Settings:
    asin: str
    max_unit_cents: int
    interval_seconds: int

    @classmethod
    def load(cls, path: Path):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        asin = data["asin"]
        if not isinstance(asin, str) or not re.fullmatch(r"[A-Z0-9]{10}", asin):
            raise ValueError("ASIN inválido.")
        limit = positive_int(data["max_unit_cents"], "Limite em centavos", 100_000_000)
        interval = positive_int(data["interval_seconds"], "Intervalo", 86400)
        if interval < 60:
            raise ValueError("Use pelo menos 60 segundos entre consultas.")
        return cls(asin, limit, interval)

    @property
    def product_url(self):
        return f"https://www.amazon.com.br/dp/{self.asin}"


@dataclass(frozen=True)
class Review:
    asin: str
    quantity: int
    total_cents: int


def check_review(review: Review, settings: Settings, quantity: int) -> None:
    if review.asin != settings.asin or review.quantity != quantity:
        raise CannotVerify("Produto ou quantidade no checkout diferem do solicitado.")
    if review.total_cents <= 0:
        raise CannotVerify("Total do pedido inválido.")
    limit = settings.max_unit_cents * quantity
    if review.total_cents > limit:
        raise CannotVerify(
            f"Total com frete de {brl(review.total_cents)} excede o teto de {brl(limit)}."
        )
