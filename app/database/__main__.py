"""`python -m app.database`: apply pending schema upgrades (compatibility entry point)."""
from . import migrate

migrate()
print('FamilyFoto database schema ready.')
