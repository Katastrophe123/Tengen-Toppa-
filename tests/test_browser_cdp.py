"""Own module: Playwright forbids nesting sync sessions, and test_apply_forms keeps one open."""

from __future__ import annotations


def test_cdp_stale_tabs_closed_and_filled_tab_survives_detach(tmp_path):
    """Regression: submit must find the freshly filled tab, never a stale one.

    Tab order after re-attaching over CDP is not guaranteed, so the runner closes stale
    tabs for the site before opening a new one; this test locks that invariant in.
    """
    import socket

    from tengen_toppa.apply import browser as br

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = br.ensure_browser(port, tmp_path / "profile", headless=True)
    try:
        with br.attached(port) as b:
            br.open_page(b, "data:text/html,<input id=x value=stale1>")
            br.open_page(b, "data:text/html,<input id=x value=stale2>")
        with br.attached(port) as b:
            assert br.close_pages(b, "data:text/html") == 2
            filled = br.open_page(b, "data:text/html,<input id=x value=stale>")
            filled.fill("#x", "fresh")
        with br.attached(port) as b:  # detached and re-attached, like apply -> submit
            page = br.find_page(b, "data:text/html")
            assert page is not None and page.input_value("#x") == "fresh"
            assert br.close_pages(b, "data:text/html") == 1
            assert br.find_page(b, "data:text/html") is None
    finally:
        if proc:
            proc.terminate()
