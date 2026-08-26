"""orders.amount_original / amount_currency — валюта оплаты до пересчёта

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-26

В заказе хранилась только сумма в рублях, а ggsel в уведомлении называет валюту неверно:
заказ 46424398 пришёл как «2.54 RUB», хотя покупатель заплатил 2.54 USD (~214 ₽) за
вариант по 203 ₽. Профит-гард сравнил себестоимость с 2,54 ₽ и отказал в выкупе выгодной
сделки. Сумму мы теперь берём из purchase/info, а эти два поля хранят исходные данные —
чтобы при следующем странном заказе не пришлось лезть в API за ответом на вопрос
«а в какой это было валюте».
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("amount_original", sa.Numeric(10, 2), nullable=True))
    op.add_column("orders", sa.Column("amount_currency", sa.String(8), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "amount_currency")
    op.drop_column("orders", "amount_original")
