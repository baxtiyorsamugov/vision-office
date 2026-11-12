-- 2026-11-12: 15 local references imported on 2026-11-06 that resemble the owner's EduSchool photo < 0.30
-- while matching only one of the owner's camera frames (~0.50): reference drift. One of them
-- (2e023d9e, Oxunjonova Robiya) produced 7 false attendance marks on 09-12.11.
-- Deactivated, not deleted (reversible: set active = true).
BEGIN;
UPDATE eduschool_reference_photos SET active = false
WHERE source = 'local' AND id IN (
  '8d1b18f6-dd50-4d4e-9f2a-0ea039dc666e',
  '84dcb26f-06c5-4cdd-a659-33a173652587',
  'd88695a5-0e5b-4fd5-a419-180e2f2eff75',
  'f66a3ea9-dcc9-4b66-82d7-ab947906517e',
  '72c2ca3c-6b47-4c28-9fa2-9ecf33f767e8',
  '3ee27be3-8388-4e2e-b0d6-7da8cf9fe461',
  'bc4b5be6-3c19-4b0e-8910-77f63c942b0f',
  '7671cd64-9c64-4d27-95b7-5ee8f8ca4dc5',
  '5bf4bd45-21ff-45af-a827-061e7bc2a634',
  '2e023d9e-6d76-4d68-bdd1-5a0dc806445c',
  '5e13e546-f3ba-466a-b200-55ab3409aff5',
  '7524fe62-d976-4064-b48c-2ef5ecf65eea',
  '22559d32-6721-40b3-b5ee-79abf62f784a',
  '233c45fd-07f7-4e67-9696-379d2a78d8be',
  'ec6af31c-de37-468f-9409-39c42b332551'
);
SELECT count(*) AS deactivated FROM eduschool_reference_photos WHERE NOT active AND id IN ('8d1b18f6-dd50-4d4e-9f2a-0ea039dc666e','84dcb26f-06c5-4cdd-a659-33a173652587','d88695a5-0e5b-4fd5-a419-180e2f2eff75','f66a3ea9-dcc9-4b66-82d7-ab947906517e','72c2ca3c-6b47-4c28-9fa2-9ecf33f767e8','3ee27be3-8388-4e2e-b0d6-7da8cf9fe461','bc4b5be6-3c19-4b0e-8910-77f63c942b0f','7671cd64-9c64-4d27-95b7-5ee8f8ca4dc5','5bf4bd45-21ff-45af-a827-061e7bc2a634','2e023d9e-6d76-4d68-bdd1-5a0dc806445c','5e13e546-f3ba-466a-b200-55ab3409aff5','7524fe62-d976-4064-b48c-2ef5ecf65eea','22559d32-6721-40b3-b5ee-79abf62f784a','233c45fd-07f7-4e67-9696-379d2a78d8be','ec6af31c-de37-468f-9409-39c42b332551');
COMMIT;
