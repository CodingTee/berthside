"""
Tests for Master Console consolidation and Workspace Switchers.
Verifies that:
1. /ui/ serves the consolidated master console with all 3 views (#viewGateway, #viewReview, #viewLifecycle).
2. /ui/ includes the top-left workspace switcher pointing to sdoc_enterprise.db and linking to /shipmail/.
3. /shipmail/ serves the dedicated OAuth desk with the workspace switcher pointing to sdoc_oauth.db and linking to /ui/.
4. /ui/gateway.html and /ui/shipments.html redirect to #gateway and #lifecycle respectively.
5. Static JS assets /ui/js/gateway.js and /ui/js/lifecycle.js are served properly.
"""

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_master_console_page():
    res = client.get("/ui/")
    assert res.status_code == 200
    html = res.text

    # Top-left Workspace switcher exists
    assert "ws-switcher" in html
    assert "wsTrigger" in html
    assert "wsDropdown" in html
    assert "sdoc_enterprise.db" in html
    assert "/shipmail/" in html

    # All 3 views exist in single page
    assert 'id="viewGateway"' in html
    assert 'id="viewReview"' in html
    assert 'id="viewLifecycle"' in html

    # Workflow navigation buttons exist
    assert 'id="navGateway"' in html
    assert 'id="navReview"' in html
    assert 'id="navLifecycle"' in html

    # Scripts are loaded
    assert "/ui/js/gateway.js" in html
    assert "/ui/js/lifecycle.js" in html
    assert "switchView" in html


def test_shipmail_desk_workspace_switcher():
    res = client.get("/shipmail/")
    assert res.status_code == 200
    html = res.text

    # Workspace switcher exists
    assert "ws-switcher" in html
    assert "wsTrigger" in html
    assert "wsDropdown" in html
    assert "sdoc_oauth.db" in html
    assert "/ui/" in html

    # Simulated demo rows should be gone
    assert "simRow" not in html


def test_gateway_and_shipments_redirects():
    """The retired page URLs are still bookmarked, so they redirect server-side.
    A JS stub would boot the retired markup and its poller before navigating."""
    res_gw = client.get("/ui/gateway.html", follow_redirects=False)
    assert res_gw.status_code == 302
    assert res_gw.headers["location"] == "/ui/#gateway"

    res_ship = client.get("/ui/shipments.html", follow_redirects=False)
    assert res_ship.status_code == 302
    assert res_ship.headers["location"] == "/ui/#lifecycle"

    # Following the redirect lands on the merged console, not on a stub page
    follow = client.get("/ui/gateway.html")
    assert follow.status_code == 200
    assert 'id="viewGateway"' in follow.text
    assert 'id="viewLifecycle"' in follow.text


def test_static_js_modules():
    res_gw_js = client.get("/ui/js/gateway.js")
    assert res_gw_js.status_code == 200
    assert "window.Gateway" in res_gw_js.text

    res_lc_js = client.get("/ui/js/lifecycle.js")
    assert res_lc_js.status_code == 200
    assert "window.Lifecycle" in res_lc_js.text
    # The module must be pure JS. It used to open with a retired page fragment,
    # whose bare `</script>` made the whole file a SyntaxError.
    assert "</script>" not in res_lc_js.text
    assert "<style" not in res_lc_js.text
    assert 'getElementById("view")' not in res_lc_js.text
