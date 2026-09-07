# ERP Adapter and Mock Contract

Vision Office uses an adapter so camera and recognition code never depend on a particular ERP implementation. Until production details are supplied, use the following mock-compatible contract.

## Authentication and transport

- Base URL: `edge_integration.base_url`.
- Authentication: `Authorization: Bearer <device_api_key>`.
- Format: UTF-8 JSON over HTTPS.
- Timeout: `edge_integration.request_timeout_seconds`.
- All write requests include an `Idempotency-Key` header.

## People synchronization

`GET /api/v1/learning-centers/persons/sync`

Query parameters:

- `limit`: page size, 1-1000.
- `offset`: zero-based page offset.
- `updated_since`: optional ISO-8601 UTC timestamp.

Response is a JSON array. Each item must contain:

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "person_type": "employee",
  "fio": "Bekzod Hayitov",
  "active": true,
  "updated_at": "2026-09-03T09:15:30Z",
  "embedding": [0.01],
  "person_photo_url": "https://erp.example.uz/photos/550e8400.jpg"
}
```

`embedding` is optional but, when supplied, must contain exactly 512 finite values. `person_photo_url` is optional when embedding is valid. If the embedding is absent or invalid, Edge downloads the photo, validates it, creates a 512-dimensional embedding and caches the result locally. Invalid photos record an explicit local status and error reason.

The Edge device synchronizes this catalog hourly by default. An operator may add approved local reference photos to a synchronized person. Those photos and embeddings are never written back to ERP and remain attached to the person after future ERP updates; each valid local photo contributes an additional local matching vector.

## Recognition event delivery

`POST /api/v1/learning-centers/access-logs`

```json
{
  "event_id": "UUID",
  "edge_device_id": "UUID",
  "person_id": "UUID",
  "person_type": "employee",
  "event_type": "entry",
  "event_time": "2026-09-03T09:15:30+05:00",
  "camera_id": "UUID",
  "similarity_score": 0.87,
  "camera_photo_path": "data/events/2026-09-03/event.jpg"
}
```

Only recognized employees are delivered to ERP. Unknown faces remain local. A successful response is any `2xx`; the ERP must make duplicate `Idempotency-Key` requests safe and return the original success result.

## Edge heartbeat

`POST /api/v1/edge/heartbeat`

```json
{
  "edge_device_id": "UUID",
  "timestamp": "2026-09-03T09:15:30+05:00",
  "overall_status": "degraded",
  "internet_status": "online",
  "erp_status": "available",
  "db_status": "db_ok",
  "application_status": "application_ok",
  "cameras": [
    {"camera_id": "UUID", "status": "rtsp_connected", "last_frame_at": "2026-09-03T09:15:29+05:00"}
  ]
}
```

The Edge queues heartbeat failures locally only when they represent a status transition or periodic aggregate, not every five-second success.

## Retry rules

- Retry: network failures and HTTP `429`, `500`, `502`, `503`, `504`.
- Do not retry automatically: malformed payloads, `401`, `403`, `404`, `422`.
- Backoff: bounded exponential delay, maximum five minutes; persist attempts, error text and next attempt.
- The local outbox survives restart and sends oldest pending events first.

## Replacing the mock with production ERP

1. Keep the normalized adapter input/output models unchanged.
2. Replace endpoint paths, auth strategy and field mappings in the adapter configuration layer only.
3. Add captured ERP success/error examples as adapter tests before enabling production mode.
4. Configure the production base URL and device key only in the ignored `config/settings.yaml`.
