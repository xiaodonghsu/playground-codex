# ThingsBoard + MSFusion API (FastAPI)

该服务整合了两类能力：

1. **ThingsBoard 设备查询与批量 RPC**
2. **MSFusion 拼接屏场景上一/下一切换（内存指针）**

## 安装

```bash
pip install -r requirements.txt
```

## 环境变量（.env）

项目根目录使用 `.env`，并通过 `python-dotenv` 自动加载。

```env
TB_BASE_URL=http://127.0.0.1:8080
TB_USERNAME=tenant@thingsboard.org
TB_PASSWORD=tenant
LOG_LEVEL=INFO

MSFUSION_WS_URL=ws://192.168.31.70:6973/
MSFUSION_USER=admin
MSFUSION_PWD=admin
SCENE_ORDER_NAMES=一张图片,场景B,场景C
SCENE_ORDER_IDS=
AUTO_SWITCH_TO_FIRST=false
```

> 生产建议：场景指针是进程内状态，建议单进程运行（`--workers 1`）。

## 运行

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

## CORS

已默认启用跨域配置：
- `allow_origins=["*"]`
- `allow_methods=["*"]`
- `allow_headers=["*"]`

## ThingsBoard API

### 查询设备

`GET /devices/search`

参数：
- `device_type`（可选）
- `name_contains`（可选）
- `page_size`（默认 100）
- `page`（默认 0）

### 批量下发 RPC

`POST /rpc/batch`

请求示例：

```json
{
  "device_ids": ["d1", "d2"],
  "device_types": ["Thermostat", "Gateway"],
  "name_contains": "A",
  "one_way": false,
  "method": "setTemperature",
  "params": {"value": 23},
  "timeout": 5000
}
```

说明：
- `device_ids` 可直接指定设备 ID；
- 可与 `device_types` / `name_contains` 合并匹配，最终去重后下发。

## MSFusion Scene API

- `POST /scene/next`：切换到下一个场景（循环）
- `POST /scene/prev`：切换到上一个场景（循环）
- `GET /scene/info`：查看当前内存指针和实时播放列表
- `POST /scene/reset?do_switch=false`：重置指针（可选立即切到第一个）
- `GET /health`：健康检查与配置摘要

## 日志

系统在主要节点输出日志（登录、查询、场景切换、批量下发、异常等），可通过 `.env` 中 `LOG_LEVEL` 调整级别。
