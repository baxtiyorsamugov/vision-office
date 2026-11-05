-- Reviewed 2026-11-04: 28 local reference photos that match another person better than their own.
-- Deactivated, not deleted (reversible: set active = true).
BEGIN;
UPDATE eduschool_reference_photos SET active = false
WHERE source = 'local' AND id IN (
  '1a1e2e5d-f664-4125-8f45-469b06aa5881',
  '083d1ddd-c083-4beb-b3bc-ee7f7cc6376e',
  'f8570681-d673-4e4d-b2f4-89cd3232d40f',
  '9a08dde0-ef9b-4c1c-87b8-140393220609',
  '96ea1711-1c7a-4e8b-a888-372fed05171c',
  'f65ae285-0d00-4820-988a-6d47378cdc96',
  '774de7e9-3f43-4fe8-886b-291048e18a3b',
  'c52b428d-9ba1-4e32-ba43-b4341379caa1',
  'b73b609e-4b7c-4723-89e2-e86d2fdd0a1f',
  'b939b6ed-5a2f-4fdc-ba42-161a93db0efc',
  '5ad7d1eb-3de1-49b4-97c0-05e28895be85',
  'b75ee98c-41dc-4ddb-8139-7c5d508864a7',
  '294a3b73-a46b-40a4-9b33-f0ae95e0ec5a',
  '22560273-5de9-4b97-9394-feea41b3f0bd',
  'c5ab65e5-1039-4a9a-a213-dab19f75176a',
  'f3aa4e06-ef22-4474-8df5-5eb01e09c3bc',
  '1be6494c-2e61-485c-b111-8d50205694d3',
  '2f6ae128-232d-4f99-be2f-24fa7a87e56a',
  '9ce7ac10-96fa-42f5-a7be-c859b67bcd1e',
  'f9ce98ec-e7c6-41b5-8711-cc3cdc904399',
  '8c31e55f-c793-44ea-b157-8ba7fe295589',
  'c6846db8-8bbe-46e0-a3c7-82d9182315d2',
  '8176a7b3-a48d-4220-87b2-3e6c63524e06',
  '3bee3e43-20f4-4c31-8d5e-986129cf3bfc',
  'ad66a376-5346-4de8-b652-eb17de92216a',
  '5977425c-f63b-4de8-abc1-1e07d46448c8',
  'ec9a33ea-a06a-49de-8628-670b8c62c59d',
  '6dbb2a60-2fea-4959-88ca-bd3cf9730c03'
);
SELECT count(*) AS deactivated FROM eduschool_reference_photos WHERE NOT active AND id IN ('1a1e2e5d-f664-4125-8f45-469b06aa5881','083d1ddd-c083-4beb-b3bc-ee7f7cc6376e','f8570681-d673-4e4d-b2f4-89cd3232d40f','9a08dde0-ef9b-4c1c-87b8-140393220609','96ea1711-1c7a-4e8b-a888-372fed05171c','f65ae285-0d00-4820-988a-6d47378cdc96','774de7e9-3f43-4fe8-886b-291048e18a3b','c52b428d-9ba1-4e32-ba43-b4341379caa1','b73b609e-4b7c-4723-89e2-e86d2fdd0a1f','b939b6ed-5a2f-4fdc-ba42-161a93db0efc','5ad7d1eb-3de1-49b4-97c0-05e28895be85','b75ee98c-41dc-4ddb-8139-7c5d508864a7','294a3b73-a46b-40a4-9b33-f0ae95e0ec5a','22560273-5de9-4b97-9394-feea41b3f0bd','c5ab65e5-1039-4a9a-a213-dab19f75176a','f3aa4e06-ef22-4474-8df5-5eb01e09c3bc','1be6494c-2e61-485c-b111-8d50205694d3','2f6ae128-232d-4f99-be2f-24fa7a87e56a','9ce7ac10-96fa-42f5-a7be-c859b67bcd1e','f9ce98ec-e7c6-41b5-8711-cc3cdc904399','8c31e55f-c793-44ea-b157-8ba7fe295589','c6846db8-8bbe-46e0-a3c7-82d9182315d2','8176a7b3-a48d-4220-87b2-3e6c63524e06','3bee3e43-20f4-4c31-8d5e-986129cf3bfc','ad66a376-5346-4de8-b652-eb17de92216a','5977425c-f63b-4de8-abc1-1e07d46448c8','ec9a33ea-a06a-49de-8628-670b8c62c59d','6dbb2a60-2fea-4959-88ca-bd3cf9730c03');
COMMIT;
