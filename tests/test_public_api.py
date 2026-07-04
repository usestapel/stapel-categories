"""The package import must stay Django-free (library-standard §3.10): a bare
``import stapel_categories`` must not require configured settings, so tooling
can import it outside a Django process."""
import subprocess
import sys


def test_import_is_django_free():
    # Fresh interpreter, no DJANGO_SETTINGS_MODULE, settings never configured.
    code = (
        "import stapel_categories;"
        "assert stapel_categories.categories_settings is not None;"
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_lazy_export_surface():
    import stapel_categories

    assert "categories_settings" in dir(stapel_categories)
