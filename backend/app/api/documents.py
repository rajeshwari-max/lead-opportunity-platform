"""Legacy signed document links redirect only to discovered source PDFs.
Missing documents return 404; generated summaries are not substituted.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database.db import get_db
from app.database.models import Opportunity
from app.services.opportunity_pdf import brief_signature_ok

router = APIRouter(tags=["documents"])


@router.get("/brief/{opportunity_id}.pdf")
def opportunity_brief_pdf(opportunity_id: int, sig: str = Query(""),
                          db: Session = Depends(get_db)) -> Response:
    if not brief_signature_ok(opportunity_id, sig):
        raise HTTPException(404, "Not found")
    opportunity = db.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(404, "Not found")
    if opportunity.pdf_status == "found" and opportunity.pdf_url:
        # The source has its own PDF now; send people there.
        from fastapi.responses import RedirectResponse
        return RedirectResponse(opportunity.pdf_url, status_code=302)
    raise HTTPException(404, "Official document unavailable")
