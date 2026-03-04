# ThingsBoard 批量 RPC REST API (FastAPI)

基于 `python + fastapi + tb_rest_client.rest_client_ce` 的简单服务，提供：

1. 指定设备类型 + 设备名称片段查询设备；
2. 按一批类型（可选）和/或设备名称片段，批量下发 RPC。

## 安装

```bash
pip install -r requirements.txt
```

## 环境变量（.env）

项目根目录已提供 `.env`，并在代码中通过 `python-dotenv` 自动加载。

```env
TB_BASE_URL=http://127.0.0.1:8080
TB_USERNAME=tenant@thingsboard.org
TB_PASSWORD=tenant
```

## 运行

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## CORS

已默认启用跨域配置（`allow_origins=["*"]`、`allow_methods=["*"]`、`allow_headers=["*"]`）。

## API

### 1) 查询设备

`GET /devices/search`

查询参数：
- `device_type`: 设备类型（可选）
- `name_contains`: 名称包含关键字（可选）
- `page_size`: 分页大小（默认 100）
- `page`: 页码（默认 0）

示例：

```bash
curl "http://127.0.0.1:8000/devices/search?device_type=Thermostat&name_contains=A"
```

### 2) 批量下发 RPC

`POST /rpc/batch`

请求体示例：

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
- `device_ids` 可直接指定设备ID列表；
- `device_types` 可省略，为空时按 `name_contains` 在全部类型中检索；
- 当 `device_ids` 与筛选条件同时存在时，结果会合并去重后批量下发；
- `one_way=true` 走单向 RPC；否则走双向 RPC。
