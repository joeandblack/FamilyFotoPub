"""Keep python -m fastbackup working for existing operator commands."""
from app.fastbackup.__main__ import main


if __name__ == '__main__':
    raise SystemExit(main())
