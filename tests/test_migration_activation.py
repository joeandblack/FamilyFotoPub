import pytest

from app.migration.activation import ACCEPTANCE_CHECKS, acceptance_checks


def test_acceptance_is_explicit_and_complete():
    assert acceptance_checks(list(ACCEPTANCE_CHECKS)) == sorted(ACCEPTANCE_CHECKS)


@pytest.mark.parametrize('accepted', [None, True, 'all', [], ['library'], [1],
                                     sorted(ACCEPTANCE_CHECKS) + ['unknown']])
def test_incomplete_or_unrecognized_acceptance_is_not_enough(accepted):
    with pytest.raises(ValueError):
        acceptance_checks(accepted)
