"""${message}

Ревизия: ${up_revision}
Предыдущая: ${down_revision | comma,n}
Создана: ${create_date}

Что и зачем меняем — писать здесь, человеческим языком. Через полгода
`upgrade()` покажет ЧТО произошло, но не ответит ПОЧЕМУ; ответ должен
быть тут.

Правило: `downgrade()` обязан возвращать схему в прежний вид. Если
откат невозможен без потери данных — это пишется в тексте выше явно,
а не выясняется в момент отката.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
