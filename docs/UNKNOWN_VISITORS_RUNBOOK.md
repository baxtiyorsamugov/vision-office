# Unknown Visitors Runbook

## Purpose

The **Unknown** tab groups repeated local unknown-face observations into review cards. It is an operator aid, not proof of identity. Photos, face embeddings, candidate cards and visit history remain on the Edge device; no unknown-person data is sent to ERP.

## How It Works

- The camera writes a normal immutable unknown recognition event and a pending local embedding.
- The `unknown-clusterer` container has no RTSP connection. It clusters pending samples in the background with a conservative similarity threshold of `0.55`.
- A visit is one or more observations from the same camera and direction inside a ten-minute window.
- On first deployment the worker slowly derives embeddings from retained unknown event photos. It processes one photo every five seconds and is limited to 0.5 CPU, preserving headroom for the camera worker.

## Operator Workflow

1. Open `http://127.0.0.1:8501`, choose **Employees**, then **Unknown**.
2. Filter the table by state and open an `Unknown #` card.
3. Review first/last seen time, visit count, event history and photos. Do not treat automatic grouping as confirmed identity.
4. When identity is confirmed, choose **Create local employee**, enter name and role, and choose the clearest image. The photo is copied into the local employee profile and selected templates become local FaceID vectors.
5. Past events retain their original unknown audit record. Future recognitions match the new local employee, write local attendance and are never delivered to ERP.
6. Use **Split selected** if different people were grouped together. Use **Merge cards** only after reviewing both galleries.

## Privacy and Retention

`unknown_visitors.retention_days` defaults to 30. At expiry, unknown observation embeddings are removed and cards without current samples become archived. Existing event metadata stays as audit history, consistent with the platform event-retention policy. A converted employee retains only the employee profile deliberately created by the operator.

Do not put unknown-face photos, database dumps or API output in public storage. The local read-only API exposes this data only through the same API-key protection as the other Edge endpoints.

## Diagnostics

```powershell
docker compose ps
docker compose logs --tail 100 -f unknown-clusterer
Invoke-RestMethod http://127.0.0.1:8000/api/v1/unknown-visitors
```

`unknown-clusterer` should become `healthy`. Its latest local state is in `data/unknown_clusterer_status.json`; it contains only counters and no embeddings. If it is stopped, the camera and all existing recognition flows continue normally. Start it again with:

```powershell
docker compose up -d unknown-clusterer
```

## Local Settings

Keep these values in ignored `config/settings.yaml` only when changing defaults is needed:

```yaml
unknown_visitors:
  enabled: true
  similarity_threshold: 0.55
  visit_gap_seconds: 600
  retention_days: 30
  poll_interval_seconds: 5
  backfill_batch_size: 1
```

Increase the similarity threshold when separate people are being combined. Lower it only after reviewing representative photos, because it increases the risk of grouping different people together.
