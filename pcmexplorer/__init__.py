"""PCM Explorer -- read-only browser for Porsche PCM / Audi MMI disk images."""

__version__ = "0.2.0"


def build_id():
    """Short identifier for the exact build, or '' when running from source.

    CI writes ``_build.py`` just before packaging, so a downloaded exe can say
    precisely which commit it came from -- the first thing worth knowing when
    someone reports that an image will not open.
    """
    try:
        from ._build import COMMIT            # written by CI, absent in git
        return COMMIT
    except Exception:
        return ""


def version_string():
    """'0.2.0' from source, '0.2.0 (build 1a2b3c4)' for a CI build."""
    b = build_id()
    return "%s (build %s)" % (__version__, b) if b else __version__
