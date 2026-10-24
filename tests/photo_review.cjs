const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const assert = require("node:assert/strict");
const { JSDOM, VirtualConsole } = require("jsdom");
const errors = [];
const report = {
  version: 1,
  snapshot_id: "fixture",
  dump_sha256: "dump",
  skipped: { events: 7 },
  people: [
    {
      id: "student:one",
      full_name: "First person",
      active: true,
      person_type: "student",
      references: [{ image: "media/ref.jpg" }],
    },
    {
      id: "employee:two",
      full_name: "Second person",
      active: true,
      person_type: "employee",
      references: [],
    },
    {
      id: "student:old",
      full_name: "Inactive person",
      active: false,
      person_type: "student",
      references: [],
    },
  ],
  events: Array.from({ length: 29 }, (_, i) => ({
    id: `event-${i}`,
    camera: i % 2 ? "exit" : "entry",
    kind: "entry",
    at: new Date(Date.UTC(2026, 9, 12, 0, 0, 29 - i)).toISOString(),
    image: `media/${i}.jpg`,
    sha256: `hash-${i}`,
    width: i === 28 ? 50 : 140,
    height: 180,
    prediction_type: i === 0 ? "eduschool_student" : "unknown",
    prediction_name: i === 0 ? "First person" : null,
    prediction_person_id: i === 0 ? "student:one" : null,
  })),
};
function htmlFor(data) {
  return fs
    .readFileSync("tools/templates/photo_review.html", "utf8")
    .replace(
      "__STYLE__",
      fs.readFileSync("tools/templates/photo_review.css", "utf8"),
    )
    .replace(
      "__SCRIPT__",
      fs.readFileSync("tools/templates/photo_review.js", "utf8"),
    )
    .replace(
      "__REPORT_JSON__",
      JSON.stringify(data).replaceAll("<", "\\u003c"),
    );
}
function open(html, saved, denied = false, backup = null, blockBackup = false) {
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(e.message));
  return new JSDOM(html, {
    url: "https://offline.invalid",
    runScripts: "dangerously",
    virtualConsole: vc,
    beforeParse(w) {
      w.URL.createObjectURL = () => "blob:fixture";
      w.URL.revokeObjectURL = () => {};
      w.HTMLAnchorElement.prototype.click = () => {};
      w.HTMLDialogElement.prototype.showModal = function () {
        this.open = true;
      };
      w.HTMLDialogElement.prototype.close = function () {
        this.open = false;
      };
      if (saved)
        w.localStorage.setItem("vision-office-photo-review-v1:fixture", saved);
      if (backup)
        w.localStorage.setItem(
          "vision-office-photo-review-v1:fixture:before-name-candidates-v1",
          backup,
        );
      if (blockBackup) {
        const set = w.Storage.prototype.setItem;
        w.Storage.prototype.setItem = function (key, value) {
          if (key.endsWith(":before-name-candidates-v1"))
            throw new w.DOMException("quota", "QuotaExceededError");
          return set.call(this, key, value);
        };
      }
      if (denied)
        Object.defineProperty(w, "localStorage", {
          get() {
            throw new w.DOMException("denied", "SecurityError");
          },
        });
    },
  });
}
const dom = open(htmlFor(report)),
  w = dom.window;
const $ = (id) => w.document.getElementById(id);
const change = (id) => $(id).dispatchEvent(new w.Event("change"));
const choose = (id) =>
  w.document.querySelector(`[data-person="${id}"]`).click();
const load = () => $("frameImage").dispatchEvent(new w.Event("load"));
const tab = (s) => w.document.querySelector(`[data-status="${s}"]`).click();
$("reviewMode").value = "manual";
change("reviewMode");
assert.equal($("queue").children.length, 24);
assert.equal($("pageInfo").textContent, "1 / 2");
assert.equal(
  $("personName").textContent,
  "Человек не выбран",
  "Known prediction must not select a person",
);
assert.equal($("confirm").disabled, true);
choose("student:one");
$("verified").checked = true;
change("verified");
assert.equal($("confirm").disabled, true, "No confirmation until image loads");
load();
assert.equal($("confirm").disabled, false);
$("confirm").click();
let exported = w.photoReview.exportSession();
assert.equal(exported.decisions["event-0"].person_id, "student:one");
assert.equal(exported.decisions["event-0"].human_confirmed, true);
assert.equal(exported.decisions["event-0"].image_sha256, "hash-0");
assert.equal(exported.applied, false);
assert.equal($("verified").checked, false);
assert.equal(
  $("personName").textContent,
  "Человек не выбран",
  "Next event must not inherit identity",
);
assert.equal($("confirm").disabled, true);
$("undo").click();
assert.equal(Object.keys(w.photoReview.exportSession().decisions).length, 0);
assert.equal($("personName").textContent, "Человек не выбран");
choose("employee:two");
load();
$("verified").checked = true;
change("verified");
choose("student:one");
assert.equal(
  $("verified").checked,
  false,
  "Changing identity revokes acknowledgement",
);
$("unsure").click();
assert.equal(
  w.photoReview.exportSession().decisions["event-0"].status,
  "unsure",
);
$("rejectReason").value = "occluded";
$("reject").click();
assert.equal(
  w.photoReview.exportSession().decisions["event-1"].reason,
  "occluded",
);
const saved = w.localStorage.getItem(w.photoReview.storageKey);
const restored = open(htmlFor(report), saved);
assert.equal(
  Object.keys(restored.window.photoReview.exportSession().decisions).length,
  2,
);
assert.equal(restored.window.document.getElementById("undo").disabled, false);
restored.window.close();
const before = JSON.stringify(w.photoReview.exportSession().decisions);
for (const mutation of [
  (s) => (s.snapshot_id = "other"),
  (s) => (s.dump_sha256 = "other"),
  (s) => (s.applied = true),
  (s) => (s.decisions["event-0"].image_sha256 = "wrong"),
  (s) => (s.decisions["unknown"] = s.decisions["event-0"]),
  (s) => {
    s.decisions["event-0"].status = "confirmed";
    s.decisions["event-0"].person_id = "student:one";
  },
  (s) => {
    s.decisions["event-0"].status = "confirmed";
    s.decisions["event-0"].human_confirmed = true;
    s.decisions["event-0"].person_id = "student:old";
  },
]) {
  const bad = JSON.parse(JSON.stringify(w.photoReview.exportSession()));
  mutation(bad);
  assert.throws(() => w.photoReview.importSession(bad));
  assert.equal(
    JSON.stringify(w.photoReview.exportSession().decisions),
    before,
    "Invalid import must be atomic",
  );
}
const incoming = JSON.parse(JSON.stringify(w.photoReview.exportSession()));
incoming.decisions["event-0"] = {
  ...incoming.decisions["event-0"],
  status: "confirmed",
  person_id: "student:one",
  human_confirmed: true,
  reference_approved: true,
};
assert.throws(() => w.photoReview.importSession(incoming), /конфликтующих/);
assert.equal(JSON.stringify(w.photoReview.exportSession().decisions), before);
w.photoReview.importSession(incoming, true);
assert.equal(
  w.photoReview.exportSession().decisions["event-0"].status,
  "confirmed",
);
assert.equal(
  w.photoReview.exportSession().decisions["event-1"].status,
  "rejected",
);
tab("confirmed");
assert.equal($("queue").children.length, 1);
assert.equal($("personName").textContent, "First person");
assert.equal($("verified").checked, false);
$("reset").click();
assert.equal($("empty").hidden, false);
tab("all");
$("camera").value = "exit";
change("camera");
assert.equal($("resultCount").textContent, "14");
$("camera").value = "";
change("camera");
$("size").value = "small";
change("size");
assert.equal($("resultCount").textContent, "1");
$("size").value = "";
change("size");
$("nextPage").click();
assert.equal($("pageInfo").textContent, "2 / 2");
assert.equal($("queue").children.length, 5);
$("personSearch").value = "nobody";
$("personSearch").dispatchEvent(new w.Event("input"));
assert.match($("people").textContent, /не найдены/);
$("personSearch").value = "";
$("personSearch").dispatchEvent(new w.Event("input"));
$("inactive").checked = true;
change("inactive");
choose("student:old");
load();
assert.equal($("verified").disabled, true);
assert.equal($("confirm").disabled, true);
$("zoom").click();
assert.equal($("lightbox").open, true);
$("closeZoom").click();
$("exportButton").click();
assert.match($("saveState").textContent, /Экспорт/);
const broken = open(htmlFor(report), "broken JSON");
assert.equal(broken.window.document.getElementById("unsure").disabled, true);
assert.equal(
  broken.window.localStorage.getItem(w.photoReview.storageKey),
  "broken JSON",
);
broken.window.close();
const denied = open(htmlFor(report), null, true);
assert.equal(denied.window.document.getElementById("unsure").disabled, false);
denied.window.document.getElementById("unsure").click();
assert.equal(
  Object.keys(denied.window.photoReview.exportSession().decisions).length,
  1,
);
assert.match(
  denied.window.document.getElementById("saveState").textContent,
  /Только в памяти/,
);
denied.window.close();
w.close();

const journalReport = JSON.parse(JSON.stringify(report));
journalReport.people[2].references = [{ image: "media/old.jpg" }];
Object.assign(journalReport.events[1], {
  prediction_type: "employee",
  prediction_name: "First person",
  prediction_person_id: null,
});
Object.assign(journalReport.events[2], {
  prediction_type: "eduschool_employee",
  prediction_name: "Second person",
  prediction_person_id: "employee:two",
});
Object.assign(journalReport.events[3], {
  prediction_type: "eduschool_student",
  prediction_name: "Inactive person",
  prediction_person_id: "student:old",
});
Object.assign(journalReport.events[5], {
  prediction_type: "eduschool_student",
  prediction_name: "Old spelling",
  prediction_person_id: "student:one",
});
Object.assign(journalReport.events[6], {
  prediction_type: "eduschool_employee",
  prediction_name: "Wrong type",
  prediction_person_id: "student:one",
});
const journal = open(htmlFor(journalReport)),
  jw = journal.window;
const j = (id) => jw.document.getElementById(id);
const jload = (id) => j(id).dispatchEvent(new jw.Event("load"));
const jtab = (status) =>
  jw.document.querySelector(`[data-status="${status}"]`).click();
const jframe = (id) =>
  jw.document.querySelector(`[data-event="${id}"]`).click();
assert.equal(j("reviewMode").value, "journal");
assert.equal(j("resultCount").textContent, "6");
assert.equal(
  jw.document.querySelector('[data-status="unreviewed"] b').textContent,
  "6",
);
assert.equal(j("personPicker").hidden, true);
assert.equal(j("personName").textContent, "First person");
assert.match(j("prediction").textContent, /личность не проверена/);
assert.equal(
  Object.keys(jw.photoReview.exportSession().decisions).length,
  0,
  "Displaying metadata is not a confirmation",
);
jload("frameImage");
assert.equal(j("confirm").disabled, true, "Reference must load too");
jload("referenceImage");
assert.equal(j("confirm").disabled, false);
assert.equal(j("confirmLabel").textContent, "Да");
j("confirm").click();
let journalSession = jw.photoReview.exportSession();
assert.equal(
  journalSession.decisions["event-0"].review_method,
  "journal_label",
);
assert.equal(
  journalSession.decisions["event-0"].journal_person_id,
  "student:one",
);
assert.equal(journalSession.decisions["event-0"].person_id, "student:one");
assert.equal(
  journalSession.decisions["event-0"].reference_approved,
  false,
  "Identity alone must not approve reference quality",
);
assert.equal(
  j("personName").textContent,
  "Карточка по ID не найдена",
  "Equal names must not create a link",
);
jload("frameImage");
assert.equal(j("confirm").disabled, true);
j("reject").click();
assert.equal(
  jw.photoReview.exportSession().decisions["event-1"].reason,
  "journal_not_confirmed",
);
assert.equal(
  jw.photoReview.exportSession().decisions["event-1"].journal_person_id,
  null,
);
assert.equal(j("personName").textContent, "Second person");
jload("frameImage");
assert.equal(j("confirm").disabled, true, "No reference means no Yes");
j("unsure").click();
assert.equal(
  jw.photoReview.exportSession().decisions["event-2"].status,
  "unsure",
);
jload("frameImage");
jload("referenceImage");
assert.equal(j("confirm").disabled, true, "Inactive card cannot be enrolled");
jtab("all");
jframe("event-6");
jload("frameImage");
assert.equal(
  j("personName").textContent,
  "Карточка по ID не найдена",
  "Person type must agree with the stored ID",
);
assert.equal(j("confirm").disabled, true);
jframe("event-5");
jload("frameImage");
assert.equal(
  j("personName").textContent,
  "First person",
  "Use exact ID, not name similarity",
);
assert.match(j("prediction").textContent, /Old spelling/);
j("referenceImage").dispatchEvent(new jw.Event("error"));
assert.equal(j("confirm").disabled, true);
jload("referenceImage");
j("verified").checked = true;
j("verified").dispatchEvent(new jw.Event("change"));
j("confirm").click();
assert.equal(
  jw.photoReview.exportSession().decisions["event-5"].reference_approved,
  true,
);
journalSession = JSON.parse(JSON.stringify(jw.photoReview.exportSession()));
for (const mutate of [
  (s) => (s.decisions["event-0"].journal_person_id = "employee:two"),
  (s) => (s.decisions["event-0"].person_id = "employee:two"),
  (s) => (s.decisions["event-1"].reference_approved = true),
]) {
  const bad = JSON.parse(JSON.stringify(journalSession));
  mutate(bad);
  assert.throws(() => jw.photoReview.importSession(bad, true));
  assert.equal(
    JSON.stringify(jw.photoReview.exportSession().decisions),
    JSON.stringify(journalSession.decisions),
  );
}
j("undo").click();
assert.equal(j("reviewMode").value, "journal");
assert.equal(jw.photoReview.exportSession().decisions["event-5"], undefined);
assert.equal(j("verified").checked, false);
const journalRestored = open(
  htmlFor(journalReport),
  jw.localStorage.getItem(jw.photoReview.storageKey),
);
assert.equal(
  journalRestored.window.photoReview.exportSession().decisions["event-0"]
    .reference_approved,
  false,
);
journalRestored.window.close();
jw.close();
const legacySession = JSON.parse(JSON.stringify(incoming));
for (const record of Object.values(legacySession.decisions)) {
  delete record.review_method;
  delete record.journal_person_id;
  delete record.reference_approved;
}
const legacy = open(
  htmlFor(report),
  JSON.stringify({ session: legacySession, history: [] }),
);
const recovered =
  legacy.window.photoReview.exportSession().decisions["event-0"];
assert.equal(recovered.review_method, "manual");
assert.equal(
  recovered.reference_approved,
  true,
  "Previous explicit manual approvals must survive the update",
);
assert.equal(legacy.window.document.getElementById("error").hidden, true);
legacy.window.close();

const nameReport = JSON.parse(JSON.stringify(report));
nameReport.people.push(
  {
    id: "employee:name-1",
    full_name: "O‘tkir Karimov",
    active: true,
    person_type: "employee",
    references: [{ image: "media/name-1.jpg" }],
  },
  {
    id: "employee:name-2",
    full_name: "Karimov Otkir Muradovich",
    active: true,
    person_type: "employee",
    references: [{ image: "media/name-2.jpg" }],
  },
  {
    id: "employee:no-ref",
    full_name: "Otkir Karimov Akmalovich",
    active: true,
    person_type: "employee",
    references: [],
  },
  {
    id: "employee:inactive-name",
    full_name: "Otkir Karimov",
    active: false,
    person_type: "employee",
    references: [{ image: "media/inactive.jpg" }],
  },
  {
    id: "student:wrong-kind",
    full_name: "Otkir Karimov",
    active: true,
    person_type: "student",
    references: [{ image: "media/wrong.jpg" }],
  },
  {
    id: "employee:one-token",
    full_name: "Another Karimov",
    active: true,
    person_type: "employee",
    references: [{ image: "media/another.jpg" }],
  },
);
Object.assign(nameReport.events[1], {
  prediction_type: "employee",
  prediction_name: "KARIMOV O`TKIR",
  prediction_person_id: null,
});
Object.assign(nameReport.events[2], {
  prediction_type: "employee",
  prediction_name: "Karimov",
  prediction_person_id: null,
});
const priorSession = JSON.parse(JSON.stringify(legacySession));
priorSession.decisions["event-1"] = {
  status: "unsure",
  person_id: null,
  image_sha256: "hash-1",
  human_confirmed: false,
  reason: null,
  decided_at: "2026-10-16T00:00:00Z",
  review_method: "journal_label",
  journal_person_id: null,
  reference_approved: false,
};
const priorRaw = JSON.stringify({
  session: priorSession,
  history: [{ id: "event-1", previous: null }],
});
const names = open(htmlFor(nameReport), priorRaw),
  nw = names.window;
const n = (id) => nw.document.getElementById(id);
const backupKey = nw.photoReview.storageKey + ":before-name-candidates-v1";
const nload = (id) => n(id).dispatchEvent(new nw.Event("load"));
const candidate = (id) =>
  nw.document.querySelector(`[data-candidate="${id}"]`).click();
assert.equal(
  nw.localStorage.getItem(nw.photoReview.storageKey),
  priorRaw,
  "Loading the update must not rewrite existing progress",
);
assert.equal(
  nw.localStorage.getItem(backupKey),
  priorRaw,
  "Backup is the exact pre-update payload including history",
);
assert.equal(n("backupButton").disabled, false);
assert.equal(nw.photoReview.nameCandidatesFor("event-1").length, 3);
assert.equal(
  nw.photoReview.nameCandidatesFor("event-2").length,
  0,
  "One matching name token is not enough",
);
assert.equal(
  nw.photoReview.nameCandidatesFor("event-0").length,
  0,
  "Existing ID link remains authoritative metadata",
);
assert.equal(nw.photoReview.nameCandidatesFor("missing").length, 0);
nw.document.querySelector('[data-status="unsure"]').click();
assert.equal(n("nameCandidateSection").hidden, false);
assert.equal(n("nameCandidates").children.length, 3);
assert.equal(
  n("personName").textContent,
  "Карточка по ID не найдена",
  "Candidates must never be auto-selected",
);
const originalDecisions = JSON.stringify(
  nw.photoReview.exportSession().decisions,
);
candidate("employee:name-1");
assert.equal(n("personName").textContent, "O‘tkir Karimov");
assert.match(n("journalWarning").textContent, /тексту ФИО/);
assert.equal(
  n("referenceHeading").textContent,
  "Эталон выбранной карточки ERP",
);
assert.equal(
  nw.localStorage.getItem(nw.photoReview.storageKey),
  priorRaw,
  "Selection must not write a decision",
);
assert.equal(
  JSON.stringify(nw.photoReview.exportSession().decisions),
  originalDecisions,
);
nload("frameImage");
nload("referenceImage");
assert.equal(n("confirm").disabled, false);
const staleLoad = n("referenceImage").onload;
candidate("employee:no-ref");
staleLoad();
assert.equal(
  n("confirm").disabled,
  true,
  "No-reference candidate must not inherit a loaded reference",
);
candidate("employee:name-2");
assert.equal(n("verified").checked, false);
assert.equal(n("confirm").disabled, true);
nload("referenceImage");
n("verified").checked = true;
n("verified").dispatchEvent(new nw.Event("change"));
n("confirm").click();
const nameDecision = nw.photoReview.exportSession().decisions["event-1"];
assert.equal(nameDecision.review_method, "name_candidate");
assert.equal(nameDecision.name_candidate_id, "employee:name-2");
assert.equal(nameDecision.person_id, "employee:name-2");
assert.equal(nameDecision.journal_person_id, null);
assert.equal(nameDecision.reference_approved, true);
assert.equal(
  nw.localStorage.getItem(backupKey),
  priorRaw,
  "New confirmations must not replace the backup",
);
const originalNormalized = JSON.parse(originalDecisions);
assert.deepEqual(
  JSON.parse(
    JSON.stringify(nw.photoReview.exportSession().decisions["event-0"]),
  ),
  originalNormalized["event-0"],
  "Unrelated ready annotations stay unchanged",
);
const currentRaw = nw.localStorage.getItem(nw.photoReview.storageKey);
const reopened = open(htmlFor(nameReport), currentRaw, false, priorRaw);
assert.equal(reopened.window.localStorage.getItem(backupKey), priorRaw);
assert.equal(
  reopened.window.photoReview.exportSession().decisions["event-1"]
    .name_candidate_id,
  "employee:name-2",
);
reopened.window.document.querySelector('[data-status="confirmed"]').click();
reopened.window.document.querySelector('[data-event="event-1"]').click();
assert.equal(
  reopened.window.document.getElementById("personName").textContent,
  "Karimov Otkir Muradovich",
);
reopened.window.close();
const invalidCandidate = JSON.parse(
  JSON.stringify(nw.photoReview.exportSession()),
);
invalidCandidate.decisions["event-1"].name_candidate_id = "employee:one-token";
invalidCandidate.decisions["event-1"].person_id = "employee:one-token";
assert.throws(() => nw.photoReview.importSession(invalidCandidate, true));
n("undo").click();
assert.equal(
  nw.photoReview.exportSession().decisions["event-1"].status,
  "unsure",
);
assert.equal(
  nw.photoReview.exportSession().decisions["event-1"].review_method,
  "journal_label",
);
assert.equal(n("personName").textContent, "Карточка по ID не найдена");
const otherTabRaw = currentRaw;
nw.localStorage.setItem(nw.photoReview.storageKey, otherTabRaw);
n("unsure").click();
assert.equal(
  nw.localStorage.getItem(nw.photoReview.storageKey),
  otherTabRaw,
  "A stale tab must not overwrite another tab's progress",
);
assert.match(n("error").textContent, /другой вкладке/);
nw.close();
const quota = open(htmlFor(nameReport), priorRaw, false, null, true);
const qw = quota.window;
qw.document.querySelector('[data-status="unsure"]').click();
qw.document.getElementById("reject").click();
assert.equal(
  qw.localStorage.getItem(qw.photoReview.storageKey),
  priorRaw,
  "Backup failure must preserve the old saved annotations",
);
assert.equal(
  qw.photoReview.exportSession().decisions["event-1"].status,
  "rejected",
  "New work remains exportable in memory",
);
assert.match(
  qw.document.getElementById("error").textContent,
  /резервную копию/,
);
qw.close();
assert.deepEqual(errors, []);
console.log("Photo review: fixture workflows and safeguards OK");
async function verifyBundle() {
  if (!process.argv[2]) return;
  const file = path.resolve(process.argv[2]),
    base = path.dirname(file);
  const html = fs.readFileSync(file, "utf8");
  const actual = JSON.parse(
    fs.readFileSync(path.join(base, "manifest.json"), "utf8"),
  );
  assert.equal(
    actual.events.length,
    new Set(actual.events.map((e) => e.id)).size,
  );
  assert.equal(
    actual.people.length,
    new Set(actual.people.map((p) => p.id)).size,
  );
  const assets = new Map(
    [...actual.events, ...actual.people.flatMap((p) => p.references)].map(
      (row) => [row.image, row.sha256],
    ),
  );
  const pending = [...assets];
  let cursor = 0,
    checked = 0;
  await Promise.all(
    Array.from({ length: 8 }, async () => {
      while (cursor < pending.length) {
        const [relative, hash] = pending[cursor++];
        assert.match(relative, /^media\/[a-f0-9]{64}\.jpg$/);
        const bytes = await fs.promises.readFile(path.join(base, relative));
        assert.equal(
          crypto.createHash("sha256").update(bytes).digest("hex"),
          hash,
        );
        if (++checked % 5000 === 0)
          console.log(`Asset hashes: ${checked}/${assets.size}`);
      }
    }),
  );
  assert(!/https?:\/\//.test(html), "Bundle should contain no remote URLs");
  const actualDom = open(html),
    aw = actualDom.window;
  assert.equal(Object.keys(aw.photoReview.exportSession().decisions).length, 0);
  const firstNamed = actual.events.find(
    (row) => row.prediction_type !== "unknown",
  );
  const expectedPerson = actual.people.find(
    (person) => person.id === firstNamed.prediction_person_id,
  );
  assert.equal(
    aw.document.getElementById("personName").textContent,
    expectedPerson?.full_name ?? "Карточка по ID не найдена",
  );
  assert.equal(
    aw.document.getElementById("resultCount").textContent.replace(/\s/g, ""),
    String(
      actual.events.filter((row) => row.prediction_type !== "unknown").length,
    ),
  );
  assert.equal(aw.document.getElementById("queue").children.length, 24);
  assert.equal(aw.document.querySelectorAll("#tabs button").length, 5);
  assert.equal(aw.document.getElementById("confirm").disabled, true);
  const missingLinks = actual.events.filter(
    (row) => row.prediction_type !== "unknown" && !row.prediction_person_id,
  );
  const withNameCandidates = missingLinks.filter(
    (row) => aw.photoReview.nameCandidatesFor(row.id).length,
  );
  console.log(
    `Text-name candidates: ${withNameCandidates.length}/${missingLinks.length} legacy events; ${new Set(withNameCandidates.map((row) => row.prediction_name)).size} old name labels`,
  );
  aw.close();
  console.log(
    `Actual bundle: ${actual.events.length} events, ${actual.people.length} people, ${assets.size} verified assets`,
  );
  assert.deepEqual(errors, []);
}
verifyBundle().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
