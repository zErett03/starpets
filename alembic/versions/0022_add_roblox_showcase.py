"""offers.roblox_showcase / offers.showcase_retired — карточки под витрину Roblox

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-28

Витрина Roblox на GGSel не берёт карточку с меню «Вариант»: блок `[roblox]` один на
карточку, а у пунктов меню свои возраст, полёт и езда. Поэтому каждый вариант снова
живёт своей карточкой (строка offers на starpets_product_id), а SKU-карточка с меню
гасится, когда все её варианты в наличии стоят отдельно.

* `roblox_showcase` — поштучная карточка оформлена под витрину (название + блок).
* `showcase_retired` — у SKU-карточки (age='__sku__'): погашена ради витрины. Такую
  не пересобирают и не включают синки SKU, иначе один питомец встанет на витрину дважды.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("offers", sa.Column("roblox_showcase", sa.Boolean(),
                                      server_default=sa.false(), nullable=False))
    op.add_column("offers", sa.Column("showcase_retired", sa.Boolean(),
                                      server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.drop_column("offers", "showcase_retired")
    op.drop_column("offers", "roblox_showcase")
