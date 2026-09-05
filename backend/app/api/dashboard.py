"""Read-only dashboard queries over published valuation versions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, over, select
from sqlalchemy.orm import Session, aliased

from app.analytics.nav import calculate_nav_series
from app.analytics.scope import COMPANY_SCOPE_TRIGGER
from app.auth.dependencies import AuthContext, get_auth_context, get_db
from app.db.base import (
    AnalysisRunStatus,
    FundStatus,
    RiskEventStatus,
    ValuationStatus,
)
from app.db.models import (
    AnalysisRun,
    CompanyMetricDaily,
    Fund,
    FundAlias,
    FundDailySnapshot,
    FundMetricDaily,
    PositionDaily,
    RiskEvent,
    ShareClass,
    ValidationResult,
    ValuationVersion,
)

router = APIRouter(tags=["dashboard"])
DatabaseSession = Annotated[Session, Depends(get_db)]
CurrentContext = Annotated[AuthContext, Depends(get_auth_context)]


@dataclass(frozen=True, slots=True)
class _VersionView:
    fund: Fund
    snapshot: FundDailySnapshot | None
    quality_status: str
    analysis_status: str
    analysis_run: AnalysisRun | None
    metric: FundMetricDaily | None


def _decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _published_versions(
    session: Session,
    *,
    fund_id: int | None = None,
    fund_ids: tuple[int, ...] | None = None,
    as_of: date | None = None,
) -> list[ValuationVersion]:
    """Return the most recent PUBLISHED version per active fund.

    The previous implementation loaded every published version into Python and
    deduplicated by fund_id there. On a real dataset that scan produced
    hundreds of thousands of rows per call, which OOMed the worker whenever
    list_funds walked the fund universe. The ROW_NUMBER() window function
    pushes the same "latest per fund" logic into SQL.
    """

    rank = over(
        func.row_number(),
        partition_by=ValuationVersion.fund_id,
        order_by=(
            ValuationVersion.valuation_date.desc(),
            ValuationVersion.id.desc(),
        ),
    )
    subquery = (
        select(ValuationVersion.id, rank.label("_rank"))
        .join(Fund, Fund.id == ValuationVersion.fund_id)
        .where(ValuationVersion.status == ValuationStatus.PUBLISHED)
        .where(Fund.status == FundStatus.ACTIVE)
    )
    if fund_id is not None:
        subquery = subquery.where(ValuationVersion.fund_id == fund_id)
    if fund_ids is not None:
        subquery = subquery.where(ValuationVersion.fund_id.in_(fund_ids))
    if as_of is not None:
        subquery = subquery.where(ValuationVersion.valuation_date == as_of)
    subquery = subquery.subquery()
    statement = (
        select(ValuationVersion)
        .join(subquery, subquery.c.id == ValuationVersion.id)
        .where(subquery.c._rank == 1)
        .order_by(ValuationVersion.fund_id)
    )
    return list(session.scalars(statement))


def _version_for_fund(
    session: Session, fund_id: int, as_of: date | None
) -> ValuationVersion | None:
    versions = _published_versions(session, fund_id=fund_id, as_of=as_of)
    return versions[0] if versions else None


def _snapshot(session: Session, version_id: int) -> FundDailySnapshot | None:
    return session.scalar(
        select(FundDailySnapshot).where(
            FundDailySnapshot.valuation_version_id == version_id
        )
    )


def _quality_status(session: Session, version_id: int) -> str:
    levels = session.scalars(
        select(ValidationResult.level).where(
            ValidationResult.valuation_version_id == version_id,
            ValidationResult.ignored.is_(False),
        )
    ).all()
    if any(str(level) == "critical" for level in levels):
        return "warning"
    if any(str(level) == "warning" for level in levels):
        return "warning"
    return "valid"


def _analysis_run_for_version(
    session: Session, version: ValuationVersion
) -> AnalysisRun | None:
    return session.scalar(
        select(AnalysisRun)
        .join(FundMetricDaily, FundMetricDaily.source_analysis_run_id == AnalysisRun.id)
        .where(
            AnalysisRun.status == AnalysisRunStatus.SUCCEEDED,
            FundMetricDaily.fund_id == version.fund_id,
            FundMetricDaily.valuation_date == version.valuation_date,
        )
        .order_by(AnalysisRun.id.desc())
        .limit(1)
    )


def _analysis_status(session: Session, version: ValuationVersion) -> str:
    latest = session.scalar(
        select(AnalysisRun)
        .join(
            ValuationVersion,
            ValuationVersion.id == AnalysisRun.trigger_version_id,
        )
        .where(
            ValuationVersion.fund_id == version.fund_id,
            AnalysisRun.input_start_date.is_not(None),
            AnalysisRun.input_start_date <= version.valuation_date,
            (AnalysisRun.input_end_date.is_(None))
            | (AnalysisRun.input_end_date >= version.valuation_date),
        )
        .order_by(AnalysisRun.id.desc())
        .limit(1)
    )
    if latest is None:
        return "pending"
    if latest.status == AnalysisRunStatus.SUCCEEDED:
        return "ready" if _analysis_run_for_version(session, version) else "stale"
    if latest.status == AnalysisRunStatus.FAILED:
        return "stale"
    return "pending"


def _metric_for_version(
    session: Session, version: ValuationVersion
) -> FundMetricDaily | None:
    run = _analysis_run_for_version(session, version)
    if run is None or run.status != AnalysisRunStatus.SUCCEEDED:
        return None
    return session.scalar(
        select(FundMetricDaily).where(
            FundMetricDaily.fund_id == version.fund_id,
            FundMetricDaily.valuation_date == version.valuation_date,
            FundMetricDaily.source_analysis_run_id == run.id,
        )
    )


def _latest_company_metric(
    session: Session, as_of: date | None, *, minimum_run_id: int | None = None
) -> tuple[CompanyMetricDaily | None, AnalysisRun | None]:
    published_date_exists = (
        select(ValuationVersion.id)
        .join(Fund, Fund.id == ValuationVersion.fund_id)
        .where(
            ValuationVersion.status == ValuationStatus.PUBLISHED,
            Fund.status == FundStatus.ACTIVE,
            ValuationVersion.valuation_date == CompanyMetricDaily.valuation_date,
        )
        .exists()
    )
    statement = (
        select(CompanyMetricDaily, AnalysisRun)
        .join(AnalysisRun, AnalysisRun.id == CompanyMetricDaily.source_analysis_run_id)
        .where(
            AnalysisRun.status == AnalysisRunStatus.SUCCEEDED,
            published_date_exists,
        )
        .order_by(
            CompanyMetricDaily.valuation_date.desc(),
            AnalysisRun.id.desc(),
            CompanyMetricDaily.id.desc(),
        )
    )
    if as_of is not None:
        statement = statement.where(CompanyMetricDaily.valuation_date == as_of)
    if minimum_run_id is not None:
        statement = statement.where(AnalysisRun.id >= minimum_run_id)
    row = session.execute(statement.limit(1)).first()
    if row is None:
        return None, None
    metric, run = row
    return metric, run


def _version_views(
    session: Session, versions: list[ValuationVersion]
) -> dict[int, _VersionView]:
    if not versions:
        return {}
    version_ids = tuple(version.id for version in versions)
    fund_ids = tuple({version.fund_id for version in versions})
    start_date = min(version.valuation_date for version in versions)
    end_date = max(version.valuation_date for version in versions)

    funds = {
        fund.id: fund
        for fund in session.scalars(select(Fund).where(Fund.id.in_(fund_ids)))
    }
    snapshots = {
        snapshot.valuation_version_id: snapshot
        for snapshot in session.scalars(
            select(FundDailySnapshot).where(
                FundDailySnapshot.valuation_version_id.in_(version_ids)
            )
        )
    }
    warning_versions = set(
        session.scalars(
            select(ValidationResult.valuation_version_id)
            .where(
                ValidationResult.valuation_version_id.in_(version_ids),
                ValidationResult.level.in_(("critical", "warning")),
                ValidationResult.ignored.is_(False),
            )
            .distinct()
        )
    )

    trigger_version = aliased(ValuationVersion)
    covering_runs: dict[int, list[AnalysisRun]] = {}
    rows = session.execute(
        select(AnalysisRun, trigger_version.fund_id)
        .join(trigger_version, trigger_version.id == AnalysisRun.trigger_version_id)
        .where(
            trigger_version.fund_id.in_(fund_ids),
            AnalysisRun.input_start_date.is_not(None),
            AnalysisRun.input_start_date <= end_date,
            (AnalysisRun.input_end_date.is_(None))
            | (AnalysisRun.input_end_date >= start_date),
        )
        .order_by(AnalysisRun.id.desc())
    )
    for run, run_fund_id in rows:
        covering_runs.setdefault(run_fund_id, []).append(run)

    metrics: dict[tuple[int, date], FundMetricDaily] = {}
    metric_runs: dict[tuple[int, date], AnalysisRun] = {}
    metric_rows = session.execute(
        select(FundMetricDaily, AnalysisRun)
        .join(AnalysisRun, AnalysisRun.id == FundMetricDaily.source_analysis_run_id)
        .where(
            FundMetricDaily.fund_id.in_(fund_ids),
            FundMetricDaily.valuation_date >= start_date,
            FundMetricDaily.valuation_date <= end_date,
            AnalysisRun.status == AnalysisRunStatus.SUCCEEDED,
        )
        .order_by(AnalysisRun.id.desc(), FundMetricDaily.id.desc())
    )
    version_keys = {(version.fund_id, version.valuation_date) for version in versions}
    for metric, run in metric_rows:
        key = (metric.fund_id, metric.valuation_date)
        if key in version_keys and key not in metrics:
            metrics[key] = metric
            metric_runs[key] = run

    views: dict[int, _VersionView] = {}
    for version in versions:
        key = (version.fund_id, version.valuation_date)
        latest = next(
            (
                run
                for run in covering_runs.get(version.fund_id, ())
                if run.input_start_date is not None
                and run.input_start_date <= version.valuation_date
                and (
                    run.input_end_date is None
                    or run.input_end_date >= version.valuation_date
                )
            ),
            None,
        )
        if latest is None:
            analysis_status = "pending"
        elif latest.status == AnalysisRunStatus.SUCCEEDED:
            analysis_status = "ready" if key in metrics else "stale"
        elif latest.status == AnalysisRunStatus.FAILED:
            analysis_status = "stale"
        else:
            analysis_status = "pending"
        views[version.id] = _VersionView(
            fund=funds[version.fund_id],
            snapshot=snapshots.get(version.id),
            quality_status="warning" if version.id in warning_versions else "valid",
            analysis_status=analysis_status,
            analysis_run=metric_runs.get(key) if analysis_status == "ready" else None,
            metric=metrics.get(key) if analysis_status == "ready" else None,
        )
    return views


@router.get("/api/v1/dashboard/overview")
def overview(
    _: CurrentContext,
    session: DatabaseSession,
    as_of: date | None = Query(default=None),  # noqa: B008
) -> dict[str, object]:
    total = (
        session.scalar(
            select(func.count(Fund.id)).where(Fund.status == FundStatus.ACTIVE)
        )
        or 0
    )
    versions = _published_versions(session, as_of=as_of)
    views = _version_views(session, versions)
    selected_fund_ids = {version.fund_id for version in versions}
    snapshots = [
        view.snapshot
        for version in versions
        if (view := views[version.id]).snapshot is not None
    ]
    total_net_assets = sum(
        (
            snapshot.net_asset_value
            for snapshot in snapshots
            if snapshot.net_asset_value is not None
        ),
        Decimal(0),
    )
    open_risk_count = (
        session.scalar(
            select(func.count(RiskEvent.id)).where(
                RiskEvent.status.in_(
                    (RiskEventStatus.OPEN, RiskEventStatus.ACKNOWLEDGED)
                )
            )
        )
        or 0
    )
    analysis_states = [views[version.id].analysis_status for version in versions]
    scope_run = session.scalar(
        select(AnalysisRun)
        .where(AnalysisRun.trigger_reason == COMPANY_SCOPE_TRIGGER)
        .order_by(AnalysisRun.id.desc())
        .limit(1)
    )
    if scope_run is not None:
        analysis_states.append(
            "ready"
            if scope_run.status == AnalysisRunStatus.SUCCEEDED
            else "stale"
            if scope_run.status == AnalysisRunStatus.FAILED
            else "pending"
        )
    analysis_status = (
        "stale"
        if "stale" in analysis_states
        else "pending"
        if "pending" in analysis_states or not analysis_states
        else "ready"
    )
    company_metric, company_run = (
        _latest_company_metric(
            session, as_of, minimum_run_id=scope_run.id if scope_run else None
        )
        if analysis_status == "ready"
        else (None, None)
    )
    return {
        "data": {
            "as_of": as_of.isoformat() if as_of else None,
            "total_net_assets": _decimal(total_net_assets) if snapshots else None,
            "fund_count": len(selected_fund_ids),
            "company_index": _decimal(company_metric.company_index)
            if company_metric
            else None,
            "company_daily_return": _decimal(company_metric.company_daily_return)
            if company_metric
            else None,
            "risk_event_count": open_risk_count,
            "quality_status": (
                "warning"
                if any(
                    views[version.id].quality_status == "warning"
                    for version in versions
                )
                else "valid"
            ),
            "funds": _overview_funds(versions, views),
        },
        "meta": {
            "as_of": as_of.isoformat() if as_of else None,
            "coverage": {"available": len(selected_fund_ids), "total": total},
            "analysis_status": analysis_status,
            "analysis_run_id": company_run.id if company_run else None,
        },
    }


def _overview_funds(
    versions: list[ValuationVersion], views: dict[int, _VersionView]
) -> list[dict[str, object]]:
    data: list[dict[str, object]] = []
    for version in versions:
        view = views[version.id]
        snapshot = view.snapshot
        metric = view.metric
        data.append(
            {
                "id": version.fund_id,
                "name": view.fund.standard_name,
                "valuation_date": version.valuation_date.isoformat(),
                "unit_nav": _decimal(snapshot.unit_nav) if snapshot else None,
                "daily_return": _decimal(
                    metric.daily_return
                    if metric is not None
                    else snapshot.daily_return
                    if snapshot
                    else None
                ),
                "analysis_status": view.analysis_status,
                "analysis_run_id": view.analysis_run.id if view.analysis_run else None,
            }
        )
    return data


@router.get("/api/v1/funds")
def list_funds(
    _: CurrentContext,
    session: DatabaseSession,
    q: str | None = Query(default=None, max_length=255),
    status: FundStatus | None = Query(default=None),  # noqa: B008
    as_of: date | None = Query(default=None),  # noqa: B008
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, object]:
    statement = select(Fund).order_by(Fund.standard_name, Fund.id)
    count_statement = select(func.count(Fund.id))
    if q:
        filter_condition = Fund.standard_name.contains(q.strip())
        statement = statement.where(filter_condition)
        count_statement = count_statement.where(filter_condition)
    if status is not None:
        statement = statement.where(Fund.status == status)
        count_statement = count_statement.where(Fund.status == status)
    total = session.scalar(count_statement) or 0
    offset = (page - 1) * page_size
    data = []
    funds = list(session.scalars(statement.offset(offset).limit(page_size)))
    fund_ids = tuple(fund.id for fund in funds)
    versions = _published_versions(session, fund_ids=fund_ids, as_of=as_of)
    versions_by_fund = {version.fund_id: version for version in versions}
    views = _version_views(session, versions)
    for fund in funds:
        version = versions_by_fund.get(fund.id)
        view = views.get(version.id) if version else None
        snapshot = view.snapshot if view else None
        metric = view.metric if view else None
        data.append(
            {
                "id": fund.id,
                "name": fund.standard_name,
                "product_code": fund.product_code,
                "status": fund.status,
                "current_version_id": version.id if version else None,
                "valuation_date": version.valuation_date.isoformat()
                if version
                else None,
                "unit_nav": _decimal(snapshot.unit_nav) if snapshot else None,
                "daily_return": _decimal(
                    metric.daily_return
                    if metric is not None
                    else snapshot.daily_return
                    if snapshot
                    else None
                ),
                "quality_status": view.quality_status if view else "pending",
                "analysis_status": view.analysis_status if view else "pending",
                "analysis_run_id": view.analysis_run.id
                if view and view.analysis_run
                else None,
            }
        )
    return {
        "data": data,
        "meta": {"page": page, "page_size": page_size, "total": total},
    }


@router.get("/api/v1/funds/{fund_id}")
def fund_detail(
    fund_id: int,
    _: CurrentContext,
    session: DatabaseSession,
    as_of: date | None = Query(default=None),  # noqa: B008
) -> dict[str, object]:
    fund = session.get(Fund, fund_id)
    if fund is None:
        raise HTTPException(status_code=404, detail="Fund not found")
    version = _version_for_fund(session, fund_id, as_of)
    analysis_status = _analysis_status(session, version) if version else "pending"
    analysis_run = (
        _analysis_run_for_version(session, version)
        if version and analysis_status == "ready"
        else None
    )
    aliases = session.scalars(
        select(FundAlias)
        .where(FundAlias.fund_id == fund.id)
        .order_by(FundAlias.match_priority.desc(), FundAlias.id)
    ).all()
    share_classes = session.scalars(
        select(ShareClass)
        .where(ShareClass.fund_id == fund.id)
        .order_by(ShareClass.share_code, ShareClass.id)
    ).all()
    return {
        "data": {
            "id": fund.id,
            "name": fund.standard_name,
            "product_code": fund.product_code,
            "strategy": fund.strategy,
            "manager": fund.manager,
            "establishment_date": fund.establishment_date,
            "notes": fund.notes,
            "aliases": [
                {
                    "id": alias.id,
                    "alias": alias.alias,
                    "source_location": alias.source_location,
                    "match_priority": alias.match_priority,
                    "valid_from": alias.valid_from,
                    "valid_to": alias.valid_to,
                }
                for alias in aliases
            ],
            "share_classes": [
                {
                    "id": share_class.id,
                    "share_code": share_class.share_code,
                    "share_name": share_class.share_name,
                    "enabled_from": share_class.enabled_from,
                    "disabled_from": share_class.disabled_from,
                    "status": ("inactive" if share_class.disabled_from else "active"),
                    "notes": share_class.notes,
                }
                for share_class in share_classes
            ],
            "status": fund.status,
            "current_version_id": version.id if version else None,
            "valuation_date": version.valuation_date.isoformat() if version else None,
            "quality_status": _quality_status(session, version.id)
            if version
            else "pending",
            "analysis_status": analysis_status,
            "analysis_run_id": analysis_run.id if analysis_run else None,
        }
    }


@router.get("/api/v1/funds/{fund_id}/nav-series")
def nav_series(
    fund_id: int,
    _: CurrentContext,
    session: DatabaseSession,
    start: date | None = Query(default=None),  # noqa: B008
    end: date | None = Query(default=None),  # noqa: B008
) -> dict[str, object]:
    if session.get(Fund, fund_id) is None:
        raise HTTPException(status_code=404, detail="Fund not found")
    # Resolve one-sided windows before validating so every query is bounded.
    effective_end = end or datetime.now(UTC).date()
    lookback = 365 * 5 if end is not None else 365
    effective_start = start or date.fromordinal(
        max(date.min.toordinal(), effective_end.toordinal() - lookback)
    )
    if effective_end < effective_start:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    if (effective_end - effective_start).days > 365 * 5:
        raise HTTPException(
            status_code=422,
            detail="窗口跨度不能超过 5 年，请缩小范围或使用导出功能",
        )
    statement = (
        select(ValuationVersion, FundDailySnapshot)
        .join(
            FundDailySnapshot,
            FundDailySnapshot.valuation_version_id == ValuationVersion.id,
        )
        .where(
            ValuationVersion.fund_id == fund_id,
            ValuationVersion.status == ValuationStatus.PUBLISHED,
            ValuationVersion.valuation_date >= effective_start,
            ValuationVersion.valuation_date <= effective_end,
        )
        .order_by(ValuationVersion.valuation_date)
    )
    rows = list(session.execute(statement))
    versions = [version for version, _ in rows]
    views = _version_views(session, versions)
    records = [
        {
            "valuation_date": version.valuation_date,
            "unit_nav": snapshot.unit_nav,
            "cumulative_unit_nav": snapshot.cumulative_unit_nav,
            "cumulative_payout": snapshot.cumulative_payout,
        }
        for version, snapshot in rows
    ]
    result = calculate_nav_series(records)
    views_by_date = {version.valuation_date: views[version.id] for version in versions}
    points: list[dict[str, object]] = []
    sources: set[str] = set()
    for point in result.points:
        view = views_by_date.get(point.valuation_date)
        analysis_status = view.analysis_status if view else "pending"
        metric = view.metric if view else None
        metric_source = "persisted" if metric is not None else "calculated_fallback"
        sources.add(metric_source)
        points.append(
            {
                "valuation_date": point.valuation_date.isoformat(),
                "unit_nav": _decimal(point.unit_nav),
                "cumulative_unit_nav": _decimal(point.cumulative_unit_nav),
                "cumulative_payout": _decimal(point.cumulative_payout),
                "adjusted_nav": _decimal(point.adjusted_nav),
                "daily_return": _decimal(
                    metric.daily_return if metric is not None else point.daily_return
                ),
                "cumulative_return": _decimal(
                    metric.cumulative_return
                    if metric is not None
                    else point.cumulative_return
                ),
                "analysis_status": analysis_status,
                "analysis_run_id": (
                    metric.source_analysis_run_id if metric is not None else None
                ),
                "metric_source": metric_source,
            }
        )
    overall_status = (
        "stale"
        if any(view.analysis_status == "stale" for view in views.values())
        else "pending"
        if any(view.analysis_status == "pending" for view in views.values())
        or not views
        else "ready"
    )
    metric_source = (
        "none"
        if not sources
        else "persisted"
        if sources == {"persisted"}
        else "calculated_fallback"
        if sources == {"calculated_fallback"}
        else "mixed"
    )
    return {
        "data": {
            "methodology": result.methodology,
            "total_return": _decimal(result.total_return),
            "points": points,
        },
        "meta": {
            "coverage": {"available": len(records), "total": len(records)},
            "analysis_status": overall_status,
            "metric_source": metric_source,
        },
    }


@router.get("/api/v1/funds/{fund_id}/positions")
def positions(
    fund_id: int,
    _: CurrentContext,
    session: DatabaseSession,
    as_of: date | None = Query(default=None),  # noqa: B008
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    if session.get(Fund, fund_id) is None:
        raise HTTPException(status_code=404, detail="Fund not found")
    version = _version_for_fund(session, fund_id, as_of)
    if version is None:
        return {"data": [], "meta": {"page": page, "page_size": page_size, "total": 0}}
    statement = (
        select(PositionDaily)
        .where(PositionDaily.valuation_version_id == version.id)
        .order_by(PositionDaily.market_value.desc(), PositionDaily.id)
    )
    offset = (page - 1) * page_size
    total = (
        session.scalar(
            select(func.count(PositionDaily.id)).where(
                PositionDaily.valuation_version_id == version.id
            )
        )
        or 0
    )
    rows = session.scalars(statement.offset(offset).limit(page_size))
    return {
        "data": [
            {
                "security_code": row.security_code,
                "security_name": row.security_name,
                "market": row.market,
                "account": row.account,
                "quantity": _decimal(row.quantity),
                "market_price": _decimal(row.market_price),
                "market_value": _decimal(row.market_value),
                "nav_weight": _decimal(row.nav_weight),
                "suspension_info": row.suspension_info,
            }
            for row in rows
        ],
        "meta": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "valuation_date": version.valuation_date.isoformat(),
        },
    }


@router.get("/api/v1/funds/{fund_id}/quality")
def quality(
    fund_id: int,
    _: CurrentContext,
    session: DatabaseSession,
    as_of: date | None = Query(default=None),  # noqa: B008
) -> dict[str, object]:
    if session.get(Fund, fund_id) is None:
        raise HTTPException(status_code=404, detail="Fund not found")
    version = _version_for_fund(session, fund_id, as_of)
    if version is None:
        return {
            "data": {"version_id": None, "validation": [], "quality_status": "pending"}
        }
    findings = session.scalars(
        select(ValidationResult)
        .where(ValidationResult.valuation_version_id == version.id)
        .where(ValidationResult.ignored.is_(False))
        .order_by(ValidationResult.level, ValidationResult.id)
    ).all()
    return {
        "data": {
            "version_id": version.id,
            "valuation_date": version.valuation_date.isoformat(),
            "quality_status": _quality_status(session, version.id),
            "validation": [
                {
                    "rule_code": finding.rule_code,
                    "level": finding.level,
                    "actual_value": _decimal(finding.actual_value),
                    "expected_value": _decimal(finding.expected_value),
                    "difference": _decimal(finding.difference),
                    "source_location": finding.source_location,
                    "message": finding.message,
                }
                for finding in findings
            ],
        }
    }
