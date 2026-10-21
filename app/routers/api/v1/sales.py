from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.sale import SaleEdition, SaleSeries
from app.services import sale_service

router = APIRouter(prefix="/sales", tags=["sales"])


@router.get("", status_code=status.HTTP_200_OK)
async def list_series(db: AsyncSession = Depends(get_db)):
    rows = (
        (await db.execute(select(SaleSeries).where(SaleSeries.publication_status == "published")))
        .scalars()
        .all()
    )
    return [{"slug": r.slug, "title": r.title} for r in rows]


@router.get("/{series_slug}", status_code=status.HTTP_200_OK)
async def get_series(series_slug: str, db: AsyncSession = Depends(get_db)):
    series = (
        await db.execute(
            select(SaleSeries).where(
                SaleSeries.slug == series_slug, SaleSeries.publication_status == "published"
            )
        )
    ).scalar_one_or_none()
    if series is None:
        raise HTTPException(status_code=404, detail="Not found")
    edition = await sale_service.current_edition(db, series)
    if edition is None or edition.publication_status != "published":
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "series": series.slug,
        "title": series.title,
        "edition": edition.slug,
        "year": edition.year,
        "lifecycle_status": edition.lifecycle_status,
    }


@router.get("/{series_slug}/{edition_slug}", status_code=status.HTTP_200_OK)
async def get_edition(series_slug: str, edition_slug: str, db: AsyncSession = Depends(get_db)):
    series = (
        await db.execute(select(SaleSeries).where(SaleSeries.slug == series_slug))
    ).scalar_one_or_none()
    if series is None or series.publication_status != "published":
        raise HTTPException(status_code=404, detail="Not found")
    edition = (
        await db.execute(
            select(SaleEdition).where(
                SaleEdition.series_id == series.id, SaleEdition.slug == edition_slug
            )
        )
    ).scalar_one_or_none()
    if edition is None or edition.publication_status != "published":
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "series": series.slug,
        "edition": edition.slug,
        "year": edition.year,
        "lifecycle_status": edition.lifecycle_status,
        "dates_confirmed": edition.dates_confirmed,
        "start_at": edition.start_at,
        "end_at": edition.end_at,
        "content_ref": f"sale:{series.slug}:{edition.slug}",
    }
