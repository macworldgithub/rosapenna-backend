"""Safety checks for browser navigation to the Rosapenna BRS site."""

from urllib.parse import urlsplit

from playwright.sync_api import Page

def validate_brs_url(value: str) -> str:
    """Permit only the Rosapenna BRS page as a browser navigation target."""
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "visitors.brsgolf.com"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and parsed.path.rstrip("/") in ("/rosapenna", "/stpatrickslinks")
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("URL must be an HTTPS Rosapenna BRS page")
    return value

def restrict_browser_navigation(page: Page):
    """Block main-frame redirects or links away from the approved BRS host."""
    def guard(route):
        request = route.request
        if request.is_navigation_request() and request.frame == page.main_frame:
            try:
                validate_brs_url(request.url)
            except ValueError:
                route.abort()
                return
        route.continue_()

    page.route("**/*", guard)
