import polar_recorder


def test_version():
    assert polar_recorder.__version__ == "0.1.0"


from pathlib import Path


def test_page_has_the_redesigned_structure():
    html = (Path(polar_recorder.__file__).parent / "static" / "index.html").read_text()
    for needle in ('id="stepper"', 'id="banner"', 'id="action"', 'id="live"', 'id="recordings"',
                   "Scan for strap", "Show in Finder", "Change folder", "function deriveState"):
        assert needle in html, needle
