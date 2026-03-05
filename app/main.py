import asyncio
import itertools
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import websockets
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from tb_rest_client.rest_client_ce import RestClientCE

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("app")


class DeviceSummary(BaseModel):
    id: str
    name: str
    type: str | None = None
    label: str | None = None


class DeviceSearchRequest(BaseModel):
    device_type: str | None = Field(default=None, description="设备类型")
    name_contains: str | None = Field(default=None, description="设备名称包含关键字")
    page_size: int = Field(default=100, ge=1, le=1000)
    page: int = Field(default=0, ge=0)


class RpcBatchRequest(BaseModel):
    device_ids: list[str] | None = Field(default=None, description="设备ID列表，可与筛选条件组合")
    device_types: list[str] | None = Field(default=None, description="设备类型列表，可为空")
    name_contains: str | None = Field(default=None, description="设备名称包含关键字")
    one_way: bool = Field(default=False, description="是否单向RPC")
    method: str = Field(..., description="RPC method")
    params: Any = Field(default_factory=dict, description="RPC params")
    timeout: int = Field(default=5000, ge=1, description="双向RPC超时(毫秒)")


class RpcResult(BaseModel):
    device_id: str
    device_name: str
    success: bool
    response: Any | None = None
    error: str | None = None


class RpcBatchResponse(BaseModel):
    matched_count: int
    success_count: int
    failed_count: int
    results: list[RpcResult]


@dataclass(frozen=True)
class Scene:
    id: str
    name: str


@dataclass(frozen=True)
class SceneConfig:
    ws_url: str
    username: str
    password: str
    order_by_names: list[str] | None = None
    order_by_ids: list[str] | None = None
    auto_switch_to_first_on_start: bool = False


class MSFusionClient:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws: Any | None = None
        self._id_gen = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._recv_task: asyncio.Task | None = None

    async def __aenter__(self):
        logger.info("MSFusion ws connect: %s", self.ws_url)
        self.ws = await websockets.connect(self.ws_url)
        self._recv_task = asyncio.create_task(self._recv_loop())
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.ws:
            await self.ws.close()
        if self._recv_task:
            self._recv_task.cancel()

    async def _recv_loop(self):
        assert self.ws is not None
        async for raw in self.ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            mid = msg.get("id")
            if mid == 0:
                continue

            fut = self._pending.pop(mid, None)
            if fut and not fut.done():
                fut.set_result(msg)

    async def call(self, action: int, params: Any, timeout: float = 8.0) -> dict[str, Any]:
        assert self.ws is not None
        mid = next(self._id_gen)
        req = {"id": mid, "action": action, "params": params}

        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut

        await self.ws.send(json.dumps(req, ensure_ascii=False))
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(mid, None)

        return resp

    async def login(self, username: str, password: str) -> None:
        resp = await self.call(257, {"user": username, "pwd": password}, timeout=5.0)
        status = (resp.get("result") or {}).get("status")
        if status != 0:
            raise RuntimeError(f"MSFusion login failed, status={status}")

    async def list_scenes(self) -> list[Scene]:
        resp = await self.call(
            4097,
            [
                {"type": "desktop", "detail": True},
                {"type": "signal", "detail": False},
                {"type": "scene", "detail": False},
                {"type": "plan", "detail": False},
            ],
            timeout=8.0,
        )
        resources = (resp.get("result") or {}).get("resource", [])
        scene_list = []
        for resource in resources:
            if str(resource.get("type", "")).lower() == "scene":
                scene_list = resource.get("list", []) or []
                break

        out: list[Scene] = []
        for item in scene_list:
            sid = item.get("id")
            if sid:
                out.append(Scene(id=str(sid), name=str(item.get("name", ""))))
        return out

    async def switch_scene(self, scene_id: str) -> int:
        resp = await self.call(12289, [{"id": scene_id, "type": "scene"}], timeout=12.0)
        status = (resp.get("result") or {}).get("status")
        if status is None:
            raise RuntimeError("Switch scene: no status in response")
        return int(status)


class SceneStateMemory:
    def __init__(self):
        self._lock = asyncio.Lock()
        self.current_index = 0


class SceneService:
    def __init__(self, cfg: SceneConfig, state: SceneStateMemory):
        self.cfg = cfg
        self.state = state

    def _build_playlist(self, all_scenes: list[Scene]) -> list[Scene]:
        if not all_scenes:
            return []

        by_id = {scene.id: scene for scene in all_scenes}
        by_name: dict[str, Scene] = {}
        for scene in all_scenes:
            if scene.name not in by_name:
                by_name[scene.name] = scene

        if self.cfg.order_by_names:
            playlist: list[Scene] = [by_name[name] for name in self.cfg.order_by_names if name in by_name]
            return playlist or all_scenes

        if self.cfg.order_by_ids:
            playlist = [by_id[sid] for sid in self.cfg.order_by_ids if sid in by_id]
            return playlist or all_scenes

        return all_scenes

    async def fetch_playlist(self) -> list[Scene]:
        async with MSFusionClient(self.cfg.ws_url) as client:
            await client.login(self.cfg.username, self.cfg.password)
            scenes = await client.list_scenes()
        playlist = self._build_playlist(scenes)
        logger.info("scene playlist fetched, total=%s ordered=%s", len(scenes), len(playlist))
        return playlist

    async def _switch_to(self, scene: Scene) -> dict[str, Any]:
        async with MSFusionClient(self.cfg.ws_url) as client:
            await client.login(self.cfg.username, self.cfg.password)
            status = await client.switch_scene(scene.id)
        logger.info("scene switched: id=%s name=%s status=%s", scene.id, scene.name, status)
        return {"scene_id": scene.id, "scene_name": scene.name, "switch_status": status}

    async def reset(self, do_switch: bool = False) -> dict[str, Any]:
        async with self.state._lock:
            self.state.current_index = 0
            logger.info("scene index reset to 0, do_switch=%s", do_switch)
            if not do_switch:
                return {"ok": True, "current_index": 0, "switched": False}

            playlist = await self.fetch_playlist()
            if not playlist:
                raise RuntimeError("No scenes available")
            result = await self._switch_to(playlist[0])
            return {"ok": True, "current_index": 0, "switched": True, **result}

    async def next_scene(self) -> dict[str, Any]:
        async with self.state._lock:
            playlist = await self.fetch_playlist()
            if not playlist:
                raise RuntimeError("No scenes available")

            if len(playlist) == 1:
                self.state.current_index = 0
                scene = playlist[0]
                return {
                    "changed": False,
                    "current_index": 0,
                    "scene_id": scene.id,
                    "scene_name": scene.name,
                    "message": "Only one scene in playlist, no switch performed.",
                    "playlist_size": 1,
                }

            self.state.current_index %= len(playlist)
            prev_index = self.state.current_index
            self.state.current_index = (self.state.current_index + 1) % len(playlist)
            cur_index = self.state.current_index
            result = await self._switch_to(playlist[cur_index])
            return {
                "changed": True,
                "previous_index": prev_index,
                "current_index": cur_index,
                "playlist_size": len(playlist),
                **result,
            }

    async def prev_scene(self) -> dict[str, Any]:
        async with self.state._lock:
            playlist = await self.fetch_playlist()
            if not playlist:
                raise RuntimeError("No scenes available")

            if len(playlist) == 1:
                self.state.current_index = 0
                scene = playlist[0]
                return {
                    "changed": False,
                    "current_index": 0,
                    "scene_id": scene.id,
                    "scene_name": scene.name,
                    "message": "Only one scene in playlist, no switch performed.",
                    "playlist_size": 1,
                }

            self.state.current_index %= len(playlist)
            prev_index = self.state.current_index
            self.state.current_index = (self.state.current_index - 1) % len(playlist)
            cur_index = self.state.current_index
            result = await self._switch_to(playlist[cur_index])
            return {
                "changed": True,
                "previous_index": prev_index,
                "current_index": cur_index,
                "playlist_size": len(playlist),
                **result,
            }


@contextmanager
def tb_client() -> RestClientCE:
    base_url = os.getenv("TB_BASE_URL")
    username = os.getenv("TB_USERNAME")
    password = os.getenv("TB_PASSWORD")

    if not all([base_url, username, password]):
        logger.error("missing TB config env vars")
        raise HTTPException(status_code=500, detail="ThingsBoard 配置缺失，请设置 TB_BASE_URL/TB_USERNAME/TB_PASSWORD")

    logger.info("initializing ThingsBoard client, base_url=%s", base_url)
    client = RestClientCE(base_url=base_url)
    try:
        client.login(username=username, password=password)
        logger.info("ThingsBoard login success, username=%s", username)
        yield client
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("ThingsBoard client call failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"ThingsBoard 调用失败: {exc}") from exc
    finally:
        try:
            client.logout()
            logger.info("ThingsBoard logout success")
        except Exception:  # noqa: BLE001
            logger.warning("ThingsBoard logout failed", exc_info=True)


def _device_id_str(device: Any) -> str:
    return str(device.id.id if hasattr(device.id, "id") else device.id)


def _to_summary(device: Any) -> DeviceSummary:
    return DeviceSummary(
        id=_device_id_str(device),
        name=getattr(device, "name", ""),
        type=getattr(device, "type", None),
        label=getattr(device, "label", None),
    )


def fetch_devices(
    client: RestClientCE,
    *,
    page_size: int,
    page: int,
    device_type: str | None,
    name_contains: str | None,
) -> list[DeviceSummary]:
    logger.info(
        "fetch devices by filter: page_size=%s page=%s device_type=%s name_contains=%s",
        page_size,
        page,
        device_type,
        name_contains,
    )
    page_data = client.get_tenant_device_infos(
        page_size=page_size,
        page=page,
        text_search=name_contains,
        type=device_type,
    )
    devices = [_to_summary(device) for device in page_data.data]
    logger.info("fetch devices done, count=%s", len(devices))
    return devices


def fetch_devices_by_ids(client: RestClientCE, device_ids: list[str]) -> list[DeviceSummary]:
    logger.info("fetch devices by ids, count=%s", len(device_ids))
    devices: list[DeviceSummary] = []
    for device_id in device_ids:
        if hasattr(client, "get_device_by_id"):
            device = client.get_device_by_id(device_id)
        else:
            device = client.get_device(device_id)
        devices.append(_to_summary(device))
    logger.info("fetch devices by ids done, hit=%s", len(devices))
    return devices


def send_rpc(client: RestClientCE, *, device_id: str, req: RpcBatchRequest) -> Any:
    payload = {"method": req.method, "params": req.params}
    logger.info("send rpc start: device_id=%s one_way=%s method=%s", device_id, req.one_way, req.method)

    if req.one_way:
        if hasattr(client, "handle_one_way_device_rpc_request"):
            resp = client.handle_one_way_device_rpc_request(device_id=device_id, request_body=payload)
        else:
            resp = client.post(f"/api/plugins/rpc/oneway/{device_id}", payload)
        logger.info("send one-way rpc success: device_id=%s", device_id)
        return resp

    payload["timeout"] = req.timeout
    if hasattr(client, "handle_two_way_device_rpc_request"):
        resp = client.handle_two_way_device_rpc_request(device_id=device_id, request_body=payload)
    else:
        resp = client.post(f"/api/plugins/rpc/twoway/{device_id}", payload)
    logger.info("send two-way rpc success: device_id=%s", device_id)
    return resp


def _split_csv_env(name: str) -> list[str] | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


def load_scene_config_from_env() -> SceneConfig:
    auto_switch = os.getenv("AUTO_SWITCH_TO_FIRST", "false").strip().lower() in {"1", "true", "yes", "y"}
    return SceneConfig(
        ws_url=os.getenv("MSFUSION_WS_URL", "ws://192.168.31.70:6973/").strip(),
        username=os.getenv("MSFUSION_USER", "admin").strip(),
        password=os.getenv("MSFUSION_PWD", "admin").strip(),
        order_by_names=_split_csv_env("SCENE_ORDER_NAMES"),
        order_by_ids=_split_csv_env("SCENE_ORDER_IDS"),
        auto_switch_to_first_on_start=auto_switch,
    )


def create_app() -> FastAPI:
    app = FastAPI(title="ThingsBoard + MSFusion API", version="1.2.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    scene_cfg = load_scene_config_from_env()
    scene_state = SceneStateMemory()
    scene_service = SceneService(scene_cfg, scene_state)

    @app.on_event("startup")
    async def on_startup():
        if scene_cfg.auto_switch_to_first_on_start:
            logger.info("startup auto switch first scene enabled")
            try:
                await scene_service.reset(do_switch=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("startup auto switch failed: %s", exc)

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "ws_url": scene_cfg.ws_url,
            "has_order_names": bool(scene_cfg.order_by_names),
            "has_order_ids": bool(scene_cfg.order_by_ids),
            "current_index_in_memory": scene_state.current_index,
            "auto_switch_to_first_on_start": scene_cfg.auto_switch_to_first_on_start,
        }

    @app.get("/scene/info")
    async def scene_info():
        try:
            playlist = await scene_service.fetch_playlist()
            idx = scene_state.current_index % len(playlist) if playlist else 0
            current = playlist[idx] if playlist else None
            return {
                "playlist_size": len(playlist),
                "current_index_in_memory": scene_state.current_index,
                "current_effective_index": idx,
                "current_scene": ({"id": current.id, "name": current.name} if current else None),
                "playlist": [{"id": s.id, "name": s.name} for s in playlist],
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("scene info failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/scene/reset")
    async def scene_reset(do_switch: bool = False):
        try:
            return await scene_service.reset(do_switch=do_switch)
        except Exception as exc:  # noqa: BLE001
            logger.exception("scene reset failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/scene/next")
    async def scene_next():
        try:
            return await scene_service.next_scene()
        except Exception as exc:  # noqa: BLE001
            logger.exception("scene next failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/scene/prev")
    async def scene_prev():
        try:
            return await scene_service.prev_scene()
        except Exception as exc:  # noqa: BLE001
            logger.exception("scene prev failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/devices/search", response_model=list[DeviceSummary], summary="按类型+名称片段查询设备")
    def search_devices(req: DeviceSearchRequest = Depends()):
        logger.info("HTTP GET /devices/search called")
        with tb_client() as client:
            devices = fetch_devices(
                client,
                page_size=req.page_size,
                page=req.page,
                device_type=req.device_type,
                name_contains=req.name_contains,
            )
            logger.info("HTTP GET /devices/search completed, count=%s", len(devices))
            return devices

    @app.post("/rpc/batch", response_model=RpcBatchResponse, summary="批量下发RPC")
    def batch_rpc(req: RpcBatchRequest):
        logger.info(
            "HTTP POST /rpc/batch called: ids=%s types=%s name_contains=%s method=%s one_way=%s",
            len(req.device_ids or []),
            len(req.device_types or []),
            req.name_contains,
            req.method,
            req.one_way,
        )
        with tb_client() as client:
            all_devices: dict[str, DeviceSummary] = {}

            if req.device_ids:
                for device in fetch_devices_by_ids(client, req.device_ids):
                    all_devices[device.id] = device

            if req.device_types:
                for device_type in req.device_types:
                    devices = fetch_devices(
                        client,
                        page_size=1000,
                        page=0,
                        device_type=device_type,
                        name_contains=req.name_contains,
                    )
                    for device in devices:
                        all_devices[device.id] = device
            elif not req.device_ids:
                devices = fetch_devices(
                    client,
                    page_size=1000,
                    page=0,
                    device_type=None,
                    name_contains=req.name_contains,
                )
                for device in devices:
                    all_devices[device.id] = device

            logger.info("batch rpc matched devices=%s", len(all_devices))
            results: list[RpcResult] = []
            for device in all_devices.values():
                try:
                    rpc_resp = send_rpc(client, device_id=device.id, req=req)
                    results.append(
                        RpcResult(
                            device_id=device.id,
                            device_name=device.name,
                            success=True,
                            response=rpc_resp,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("send rpc failed: device_id=%s error=%s", device.id, exc)
                    results.append(
                        RpcResult(
                            device_id=device.id,
                            device_name=device.name,
                            success=False,
                            error=str(exc),
                        )
                    )

            success_count = sum(1 for result in results if result.success)
            resp = RpcBatchResponse(
                matched_count=len(results),
                success_count=success_count,
                failed_count=len(results) - success_count,
                results=results,
            )
            logger.info(
                "HTTP POST /rpc/batch completed: matched=%s success=%s failed=%s",
                resp.matched_count,
                resp.success_count,
                resp.failed_count,
            )
            return resp

    return app


app = create_app()
