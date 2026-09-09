import pytest
from scripts import check_model_deprecations as check


def test_valid_deprecation_table():
    text = '''| Model | Release | Shutdown | Replacement |
| `active` | Jan 1, 2026 | No shutdown date announced | |
| `old` | Jan 1, 2026 | October 01, 2026 | `new` |'''
    result = check.parse_deprecations(text)
    assert result['active'] == (None, None)
    assert result['old'][0].isoformat() == '2026-10-01'
    assert result['old'][1] == 'new'


@pytest.mark.parametrize('text', ['<html>Error</html>', '', '| `old` | release | TBD | `new` |'])
def test_document_drift_is_not_healthy(text):
    with pytest.raises(ValueError):
        check.parse_deprecations(text)


def test_missing_model_dry_run_fails(monkeypatch):
    monkeypatch.setattr(check.sys, 'argv', ['check_model_deprecations.py', '--dry-run'])
    monkeypatch.setattr(check, 'models_in_use', lambda: ['missing'])
    monkeypatch.setattr(check, 'fetch_deprecations', lambda: {'other': (None, None)})
    with pytest.raises(SystemExit) as exc:
        check.main()
    assert exc.value.code == 1


def test_cli_dry_run_network_failure_exits_nonzero(monkeypatch):
    import runpy
    from unittest.mock import Mock
    monkeypatch.setattr(check.sys, 'argv', ['check_model_deprecations.py', '--dry-run'])
    monkeypatch.setattr(check.urllib.request, 'urlopen', Mock(side_effect=OSError('offline test')))
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(check.__file__, run_name='__main__')
    assert exc.value.code == 1
