"""Recognise a website's own files, which are never an opportunity's documents."""
from __future__ import annotations

import re

# DevelopmentAid's own site-wide files (footer quality certificate, brochures)
# are not opportunity documents. They were stored as "Official source
# documents" when a detail page was read before the opportunity had rendered.
_SITE_CHROME_DOC = re.compile(
    r"iso[-_\s]*9001|qmscert|certificate\s+no\b|/api/frontend/cms/file/|"
    r"organizations?[-_ ]?ebook|privacy[-_ ]?policy|cookie[-_ ]?policy",
    re.IGNORECASE)


def is_site_chrome_document(doc) -> bool:
    """True for a stored document (dict or documents_json text) that is the
    website's own file rather than the opportunity's."""
    if isinstance(doc, dict):
        text = f"{doc.get('label', '')} {doc.get('url', '')}"
    else:
        text = str(doc or "")
    return bool(_SITE_CHROME_DOC.search(text))
