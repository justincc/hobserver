"""Shell tests: plugin registration, tab bar, root and legacy redirects."""


def test_root_redirects_to_memory_tab(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/memory/")


def test_tab_bar_lists_all_plugins(client):
    page = client.get("/memory/").get_data(as_text=True)
    assert "Memory" in page
    assert "Prompt timing" in page
    assert "/timing/" in page


def test_tab_bar_marks_active_tab(client):
    memory_page = client.get("/memory/").get_data(as_text=True)
    assert '/memory/" class="active"' in memory_page
    timing_page = client.get("/timing/").get_data(as_text=True)
    assert '/timing/" class="active"' in timing_page


def test_legacy_event_url_redirects(client):
    resp = client.get("/event/3")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/memory/event/3")


def test_legacy_fragment_url_redirects_with_query(client):
    resp = client.get("/fragment/events?since=3")
    assert resp.status_code == 302
    assert "/memory/fragment/events" in resp.headers["Location"]
    assert "since=3" in resp.headers["Location"]
