from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import sqlite3
import uuid
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .network import lan_addresses
from .runtime_config import APP_VERSION, public_location, release_info
from .ai_config import load_config as load_ai_config
from .feishu_config import load_config as load_notification_config
from .securities import LookupFailure, TencentResolver, market_for
from .store import WatchlistStore
from .quote_service import QuoteService
from .quote_collector import QuoteCollector
from .daily_service import DailyService
from .notification_service import NotificationService
from .anomaly_service import AnomalyService
from .runtime_lock import RuntimeLock
from .explanation_service import ExplanationService
from .briefing_service import BriefingService
from .quote_snapshots import QuoteSnapshots
from .legacy_archive import LegacyArchive, legacy_budget_usage
from .research_service import ResearchService
from .research_collector import ResearchCollector
from .extended_anomalies import ExtendedAnomalies

ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger("alphaproject_lab")


class AddStock(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    symbol: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


class WatchlistGroupName(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=20)


class WatchlistMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    watchlist_created_at: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=0)
    group_id: str | None = Field(default=None, max_length=64)
    is_holding: bool


class WatchlistOrderItem(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    market: Literal['sh','sz']
    symbol: str = Field(pattern=r'^[0-9]{6}$')
    watchlist_created_at: str = Field(min_length=1, max_length=64)
    metadata_version: int = Field(ge=0)


class WatchlistOrder(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[WatchlistOrderItem] = Field(max_length=200)


class RefreshDaily(BaseModel):
    model_config = ConfigDict(extra="forbid")
    watchlist_created_at: str = Field(min_length=1, max_length=64)


class NotificationSetting(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


class NotificationTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    target_fingerprint: str = Field(pattern=r"^[a-f0-9]{24}$")


class NotificationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^[0-9]{6}$")


class AnomalyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    watchlist_created_at: str = Field(min_length=1, max_length=64)
    up_threshold: str = Field(min_length=1, max_length=9)
    down_threshold: str = Field(min_length=1, max_length=9)
    enabled: bool = Field(strict=True)
    expected_version: int = Field(ge=0, strict=True)


class ExtendedRuleConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    limit_enabled: bool = False
    breakout_enabled: bool = False
    volume_enabled: bool = False
    amplitude_enabled: bool = False
    event_enabled: bool = False
    volume_ratio: str = Field(default='3', min_length=1, max_length=9)
    amplitude_pct: str = Field(default='8', min_length=1, max_length=9)
    lookback_days: int = Field(default=60, ge=5, le=120)


class ExtendedRuleRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    watchlist_created_at: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=0)
    enabled: bool
    config: ExtendedRuleConfig


class AnomalyDemo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    market: Literal["sh", "sz"]
    symbol: str = Field(pattern=r"^[0-9]{6}$")
    watchlist_created_at: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1, strict=True)
    direction: Literal["up", "down"]
    notify: bool = Field(strict=True)


class ExplanationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    retry_of: uuid.UUID | None = None


class ExplanationReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    facts_checked: bool
    citations_checked: bool
    no_invented_causes: bool


class QuoteSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    watchlist_created_at: str = Field(min_length=1, max_length=64)
    quote_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


class BriefingSetting(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: bool
    send_time: Literal['08:45', '09:00']
    expected_version: int = Field(ge=0)


class BriefingRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: uuid.UUID
    report_date: str = Field(pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
    retry_of: uuid.UUID | None = None


def error_response(status: int, code: str, message: str):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(db_path: Path | None = None, resolver=None, allowed_hosts: list[str] | None = None, quote_source=None, clock=None, daily_source=None, notification_client=None, run_monitor=True, ai_client=None, ai_config=None, research_source=None):
    store = WatchlistStore(db_path or Path(os.environ.get(
        "ALPHA_LAB_DB", str(ROOT / ".runtime" / "watchlist.sqlite3")
    )))
    source = resolver or TencentResolver()
    legacy = LegacyArchive(store, clock)
    research = ResearchService(store, research_source, clock)
    quotes = QuoteService(store, quote_source, clock)
    collector = QuoteCollector(store, quotes, clock)
    quotes.collector = collector
    daily = DailyService(store, daily_source, clock)
    research_collector = ResearchCollector(store, research, daily, clock)
    addresses = lan_addresses()
    public_url, public_path, public_host = public_location()
    release = release_info(ROOT)
    detail_base = public_url or (f'http://{addresses[0]}:{int(os.environ.get("ALPHA_LAB_PORT", "18765"))}' if addresses else None)
    notifications = NotificationService(store, store.path.parent / "feishu-config.dpapi", notification_client, clock, detail_base)
    anomalies = AnomalyService(store, quotes, notifications, clock, public_path)
    extended = ExtendedAnomalies(store, quotes, notifications, research, clock)
    anomalies.extended = extended
    explanations = ExplanationService(store, store.path.parent / "ai-config.dpapi", ai_client, clock, ai_config,
                                      research=research, legacy_budget_usage=legacy_budget_usage)
    quote_snapshots = QuoteSnapshots(store, explanations.clock, public_path)
    briefings = BriefingService(store, daily, notifications, clock, research=research)
    runtime_lock = RuntimeLock(store.path.with_suffix(".service.lock"))
    instance_id = uuid.uuid4().hex

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime_lock.acquire()
        try:
            # Public deployments must prove private configuration before any startup writes.
            if public_url:
                configured_ai=load_ai_config(explanations.config_path)
                configured_notifications=load_notification_config(notifications.config_path)
                if not configured_ai or not configured_notifications:
                    raise RuntimeError('公网运行的模型或通知配置未就绪，未启动服务、未修改学习数据。')
                explanations.config=configured_ai
            store.initialize()
            legacy.initialize()
            research.initialize()
            quotes.initialize()
            daily.initialize()
            notifications.initialize()
            anomalies.initialize()
            extended.initialize()
            research_collector.initialize()
            explanations.initialize()
            briefings.initialize()
            if run_monitor:
                collector.start()
                anomalies.start()
                briefings.start()
                research_collector.start()
            yield
        finally:
            collector.stop()
            anomalies.stop()
            briefings.stop()
            research_collector.stop()
            if (not collector.is_alive() and (not anomalies.thread or not anomalies.thread.is_alive())
                    and not briefings.is_alive() and not research_collector.is_alive() and not explanations.active):
                runtime_lock.release()
            # If a bounded network call is still finishing, retain ownership until
            # this process exits. Never let a replacement recover its active send.

    app = FastAPI(title="AlphaProject Lab · 共享自选", version=APP_VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.legacy = legacy
    app.state.research = research
    app.state.research_collector = research_collector
    app.state.extended = extended
    app.state.quotes = quotes
    app.state.collector = collector
    app.state.daily = daily
    app.state.notifications = notifications
    app.state.anomalies = anomalies
    app.state.explanations = explanations
    app.state.quote_snapshots = quote_snapshots
    app.state.briefings = briefings
    app.state.runtime_lock = runtime_lock

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if request.headers.get("x-alpha-request") != "watchlist-v1" or (origin and origin != expected):
                return error_response(403, "invalid_origin", "请求来源无法确认，请从自选页面重新操作。")
            body_limit = 65536 if request.url.path == '/api/watchlist/reorder' else 4096
            if request.headers.get("content-length", "0").isdigit() and int(request.headers.get("content-length", "0")) > body_limit:
                return error_response(413, "request_too_large", "提交内容过长，请重新输入股票代码。")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        # Lightweight Charts 5.2.1 inserts this exact attribution-logo stylesheet.
        # Permit that verified stylesheet without enabling arbitrary inline CSS.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; style-src 'self' "
            "'sha256-3pRED1tOXas1FXFoPb9TGCjmYe9XQsmO9OV23khV2nY='; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.url.path.startswith("/api/") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or ["localhost", "127.0.0.1", "[::1]", *addresses, *([public_host] if public_host else [])])

    @app.exception_handler(LookupFailure)
    async def lookup_error(request: Request, error: LookupFailure):
        return error_response(error.status, error.code, error.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        if request.url.path.startswith('/api/briefings'):
            return error_response(422, 'invalid_request', '简报请求内容不正确，请重新读取今日记录或核对定时设置。')
        if request.url.path.startswith(("/api/events", "/api/explanations", "/api/stocks", "/api/quote-snapshots")):
            return error_response(422, "invalid_request", "依据或解释请求不正确，请重新读取记录或核对输入。")
        if request.url.path.startswith("/api/anomalies"):
            return error_response(422, "invalid_request", "异动请求内容不正确，请核对阈值或刷新后重试。")
        if request.url.path.startswith("/api/notifications"):
            return error_response(422, "invalid_request", "通知请求内容不正确，请核对输入或刷新页面。")
        if request.url.path.startswith("/api/daily/"):
            return error_response(422, "invalid_request", "日线请求内容不完整，请刷新页面后重试。")
        return error_response(422, "invalid_request", "请只提交 6 位股票代码，并核对输入后重试。")

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request: Request, error: sqlite3.Error):
        logger.error("SQLite operation failed: %s", type(error).__name__)
        return error_response(503, "storage_unavailable", "暂时无法完成操作，请刷新核对已保存的数据后重试。")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "service": "AlphaProject-Lab", "version": APP_VERSION,
                "release_id": release['release_id'], "instance_id": instance_id, "pid": os.getpid(),
                "configuration_ready": {'ai': explanations.config is not None and not explanations.config_error,
                                        'notifications': notifications.config is not None and not notifications.config_error}}

    @app.get('/release.json')
    def deployed_release():
        return JSONResponse(release, headers={'Cache-Control': 'no-store'})

    @app.get("/api/access")
    def access(request: Request):
        if public_url:
            return {'mode': 'public', 'public_url': public_url+'/', 'lan_urls': [], 'release': release}
        port = request.url.port or 80
        return {"lan_urls": [f"http://{ip}:{port}" for ip in addresses], "mode": "lan", 'release': release}

    @app.get("/api/notifications")
    def read_notifications():
        return notifications.state()

    @app.post("/api/notifications/verify")
    def verify_notifications():
        return notifications.verify()

    @app.post("/api/notifications/settings")
    def configure_notifications(body: NotificationSetting):
        return notifications.set_enabled(body.enabled)

    @app.post("/api/notifications/test", status_code=202)
    def test_notifications(body: NotificationTest, background: BackgroundTasks):
        record, text = notifications.queue_test(str(body.request_id), body.target_fingerprint)
        if text is not None:
            background.add_task(notifications.process, record["id"], text)
        return {"record": record}

    @app.post("/api/notifications/{request_id}/confirm")
    def confirm_notification(request_id: uuid.UUID, body: NotificationReceipt):
        return {"record": notifications.confirm(str(request_id), body.code)}

    @app.get("/api/anomalies")
    def read_anomalies():
        result = anomalies.state()
        result['items'] = extended.attach_rules(result['items'])
        return result

    @app.get('/api/anomalies/extended/{market}/{symbol}')
    def read_extended_rule(market: str, symbol: str):
        return extended.rule(market,symbol)

    @app.put('/api/anomalies/extended/{market}/{symbol}')
    def save_extended_rule(market: str, symbol: str, body: ExtendedRuleRequest):
        return extended.save(market,symbol,body.watchlist_created_at,body.expected_version,body.enabled,body.config.model_dump())

    @app.get("/api/anomalies/events")
    def read_live_event_history(since: str, cursor: str | None = None, limit: int = 100):
        return anomalies.event_history(since, cursor, limit)

    @app.post("/api/anomalies/rules/{market}/{symbol}")
    def save_anomaly_rule(market: str, symbol: str, body: AnomalyRule):
        return {"rule": anomalies.save_rule(market, symbol, body.watchlist_created_at,
                body.up_threshold, body.down_threshold, body.enabled, body.expected_version)}

    @app.post("/api/anomalies/demo", status_code=202)
    def start_anomaly_demo(body: AnomalyDemo):
        return {"run": anomalies.queue_demo(str(body.request_id), body.market, body.symbol,
                body.watchlist_created_at, body.expected_version, body.direction, body.notify)}

    @app.get('/api/events/{event_id}')
    def read_event(event_id: uuid.UUID):
        return anomalies.event(str(event_id))

    @app.get('/api/stocks/{market}/{symbol}/explanations')
    def stock_explanations(market: str, symbol: str):
        return quote_snapshots.state(market, symbol)

    @app.post('/api/stocks/{market}/{symbol}/explanation-snapshots')
    def save_quote_snapshot(market: str, symbol: str, body: QuoteSnapshotRequest):
        snapshot, created = quote_snapshots.save(market, symbol, body.watchlist_created_at, body.quote_hash)
        return {'snapshot': snapshot, 'reused': not created}

    @app.get('/api/quote-snapshots/{snapshot_id}/explanations')
    def read_quote_explanations(snapshot_id: uuid.UUID):
        return explanations.state(str(snapshot_id), kind='quote')

    @app.post('/api/quote-snapshots/{snapshot_id}/explanations', status_code=202)
    def generate_quote_explanation(snapshot_id: uuid.UUID, body: ExplanationRequest, background: BackgroundTasks):
        record, created = explanations.queue(str(snapshot_id), str(body.request_id), body.evidence_hash,
                                             str(body.retry_of) if body.retry_of else None, kind='quote')
        if created:
            background.add_task(explanations.process, record['id'])
        return {'record': record, 'reused': not created}

    @app.get("/api/events/{event_id}/explanations")
    def read_explanations(event_id: uuid.UUID):
        return explanations.state(str(event_id))

    @app.post("/api/events/{event_id}/explanations", status_code=202)
    def generate_explanation(event_id: uuid.UUID, body: ExplanationRequest, background: BackgroundTasks):
        record, created = explanations.queue(str(event_id), str(body.request_id), body.evidence_hash,
                                             str(body.retry_of) if body.retry_of else None)
        if created:
            background.add_task(explanations.process, record['id'])
        return {"record": record, "reused": not created}

    @app.post("/api/explanations/{request_id}/review")
    def review_explanation(request_id: uuid.UUID, body: ExplanationReview):
        return {"record": explanations.confirm_review(str(request_id), body.facts_checked,
                                                       body.citations_checked, body.no_invented_causes)}

    @app.get('/api/briefings')
    def briefing_state():
        return briefings.state()

    @app.put('/api/briefings/settings')
    def briefing_setting(body: BriefingSetting):
        return briefings.set_settings(body.enabled, body.send_time, body.expected_version)

    @app.post('/api/briefings', status_code=202)
    def create_briefing(body: BriefingRequest):
        record, created = briefings.queue_manual(str(body.request_id), body.report_date,
                                                str(body.retry_of) if body.retry_of else None)
        return {'record': record, 'reused': not created}

    @app.get("/api/securities/{symbol}")
    def lookup(symbol: str):
        return source.resolve(symbol).to_dict()

    @app.get("/api/watchlist")
    def list_watchlist():
        items = store.list_items()
        return {"items": items, "total": len(items)}

    @app.get('/api/legacy/migration')
    def legacy_migration():
        return legacy.summary()

    @app.get('/api/legacy/history')
    def legacy_history(kind: str | None = None, symbol: str | None = None, limit: int = 40, cursor: str | None = None):
        return legacy.history(kind,symbol,limit,cursor)

    @app.get('/api/research')
    def read_research():
        return research.state()

    def start_research(background,market=None,symbol=None):
        result = research.state(market,symbol)
        if result.get('refresh_in_progress') or result.get('retry_after_seconds',0)>0:
            return JSONResponse(result,status_code=202)
        background.add_task(research.refresh,market,symbol)
        return JSONResponse({**result,'refresh_state':'processing','refresh_in_progress':True},status_code=202)

    @app.post('/api/research/refresh')
    def refresh_research(background: BackgroundTasks):
        return start_research(background)

    @app.get('/api/research/{market}/{symbol}')
    def read_stock_research(market: str, symbol: str):
        return research.state(market,symbol)

    @app.post('/api/research/{market}/{symbol}/refresh')
    def refresh_stock_research(market: str, symbol: str, background: BackgroundTasks):
        if not store.get(market,symbol):
            raise LookupFailure('not_in_watchlist','请先把这只股票加入自选，再获取资料。',404)
        return start_research(background,market,symbol)

    @app.get('/api/watchlist/groups')
    def list_groups():
        return {'items': store.groups()}

    @app.post('/api/watchlist/groups')
    def create_group(body: WatchlistGroupName):
        return {'item': store.create_group(body.name)}

    @app.patch('/api/watchlist/groups/{group_id}')
    def rename_group(group_id: str, body: WatchlistGroupName):
        return {'item': store.rename_group(group_id, body.name)}

    @app.delete('/api/watchlist/groups/{group_id}')
    def delete_group(group_id: str):
        return {'removed': store.delete_group(group_id)}

    @app.post('/api/watchlist/reorder')
    def reorder_watchlist(body: WatchlistOrder):
        items = store.reorder([item.model_dump() for item in body.items])
        return {'items': items, 'total': len(items)}

    @app.post('/api/watchlist/undo/{token}')
    def undo_watchlist(token: str):
        return store.undo_remove(token)

    @app.patch('/api/watchlist/{market}/{symbol}')
    def configure_watchlist(market: str, symbol: str, body: WatchlistMetadata):
        if market_for(symbol) != market:
            raise LookupFailure('invalid_symbol', '市场与股票代码不一致。', 400)
        return {'item': store.update_metadata(market,symbol,body.watchlist_created_at,body.expected_version,body.group_id,body.is_holding)}

    @app.get("/api/dashboard")
    def dashboard():
        return quotes.dashboard()

    @app.post("/api/quotes/{market}/{symbol}/refresh")
    def refresh_quote(market: str, symbol: str):
        return quotes.refresh(market, symbol)

    @app.get("/api/daily/{market}/{symbol}")
    def read_daily(market: str, symbol: str):
        return daily.get(market, symbol)

    @app.post("/api/daily/{market}/{symbol}/refresh")
    def refresh_daily(market: str, symbol: str, body: RefreshDaily):
        return daily.refresh(market, symbol, body.watchlist_created_at)

    @app.post("/api/watchlist")
    def add_watchlist(body: AddStock):
        market = market_for(body.symbol)
        existing = store.get(market, body.symbol)
        if existing:
            return {"item": existing, "created": False}
        security = source.resolve(body.symbol)
        item, created = store.add(security)
        return JSONResponse({"item": item, "created": created}, status_code=201 if created else 200)

    @app.delete("/api/watchlist/{market}/{symbol}")
    def delete_watchlist(market: str, symbol: str, watchlist_created_at: str | None = None):
        if market_for(symbol) != market:
            raise LookupFailure("invalid_symbol", "市场与股票代码不一致，请刷新列表后重试。", 400)
        return store.remove_with_undo(market, symbol, watchlist_created_at)

    dist = ROOT / "frontend" / "dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
    if (dist / "licenses").is_dir():
        app.mount("/licenses", StaticFiles(directory=dist / "licenses"), name="licenses")

    @app.get("/", include_in_schema=False)
    def index():
        if not (dist / "index.html").is_file():
            return error_response(503, "frontend_not_built", "页面尚未构建，请先运行开发环境准备入口。")
        return FileResponse(dist / "index.html")

    return app


app = create_app()
