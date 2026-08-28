"""Side-effect-only imports so Base.metadata is complete before Alembic autogenerates.
Every domain models module gets one line here, added by the task that creates it."""

from app.domain.audit import models as audit_models  # noqa: F401
from app.domain.catalog import models as catalog_models  # noqa: F401
from app.domain.identity import models as identity_models  # noqa: F401
from app.domain.qr import models as qr_models  # noqa: F401
from app.domain.settings import models as settings_models  # noqa: F401
