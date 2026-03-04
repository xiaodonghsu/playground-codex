import logging
import os
from contextlib import contextmanager
from typing import Any

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
logger = logging.getLogger("tb_batch_rpc_api")


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


def create_app() -> FastAPI:
    app = FastAPI(title="ThingsBoard Batch RPC API", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
