const assert = require("node:assert/strict");
const fs = require("node:fs");
const { JSDOM, VirtualConsole } = require("jsdom");
const row = (id, video, vector, reference_ok = true) => ({
  id,
  video,
  frame: 25,
  time: 1,
  box: [50, 50, 130, 130],
  vector,
  reference_ok,
  sharpness: 100,
  image: "media/" + id + ".jpg",
  context: "media/context.jpg",
});
const report = {
  complete: true,
  created_at: "fixture",
  source_sha256: "abc",
  threshold: 0.55,
  skipped_small: 10,
  sample_fps: 5,
  initial_reference: "a",
  videos: [
    { name: "reference.mp4", video: "media/a.mp4", width: 1920, height: 1080 },
    { name: "test.mp4", video: "media/b.mp4", width: 1920, height: 1080 },
  ],
  observations: [
    row("a", 0, [1, 0]),
    row("b", 0, [1, 0]),
    row("other", 0, [0, 1]),
    row("hit", 1, [1, 0]),
    row("unknown", 1, [0, 1]),
    row("bad", 1, null, false),
  ],
};
const errors = [],
  vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push(e.message));
let confirmed = true,
  downloads = 0;
const html = fs
  .readFileSync("tools/templates/anonymous_face_lab.html", "utf8")
  .replace("__REPORT_JSON__", JSON.stringify(report));
const dom = new JSDOM(html, {
  url: "https://offline.invalid",
  runScripts: "dangerously",
  virtualConsole: vc,
  beforeParse(w) {
    w.HTMLMediaElement.prototype.pause = () => {};
    w.confirm = () => confirmed;
    w.URL.createObjectURL = () => "blob:test";
    w.URL.revokeObjectURL = () => {};
    w.HTMLAnchorElement.prototype.click = () => {
      downloads++;
    };
  },
});
const w = dom.window,
  $ = (id) => w.document.getElementById(id),
  change = (id) => $(id).dispatchEvent(new w.Event("change"));
const api = w.AnonymousLab,
  profiles = [{ id: "test-01", refs: ["a"] }];
assert.equal(
  api.predict(report.observations[0], profiles, 0).status,
  "excluded",
);
assert.equal(
  api.predict(report.observations[3], profiles, 0).person,
  "test-01",
);
assert.equal(
  api.predict(report.observations[4], profiles, 0).status,
  "unknown",
);
assert.equal(
  api.predict(report.observations[5], profiles, 0).status,
  "no_embedding",
);
assert.equal(
  api.predict(
    report.observations[3],
    [...profiles, { id: "test-02", refs: ["b"] }],
    0,
  ).status,
  "ambiguous",
);
assert.equal(
  api.predict(report.observations[3], [{ id: "test-01", refs: ["hit"] }], 0)
    .status,
  "no_gallery",
);
assert.equal(
  api.validState(
    {
      profiles: [{ id: "test-01", refs: ["hit"] }],
      next: 2,
      truth: {},
      independent: {},
    },
    0,
  ),
  false,
);
assert.equal(
  api.validState(
    {
      profiles: [...profiles, { id: "test-02", refs: ["a"] }],
      next: 3,
      truth: {},
      independent: {},
    },
    0,
  ),
  false,
);
assert.equal($("testVideo").options.length, 1);
assert.equal($("testVideo").value, "1");
assert.equal($("prediction").textContent, "Тест-01?");
assert.equal($("video").currentTime, 1);
Object.defineProperty($("video").parentElement, "clientWidth", { value: 960 });
Object.defineProperty($("video").parentElement, "clientHeight", { value: 540 });
$("video").dispatchEvent(new w.Event("timeupdate"));
assert.equal($("liveBoxes").children.length, 3);
$("video").currentTime = 2;
$("video").dispatchEvent(new w.Event("timeupdate"));
assert.equal($("liveBoxes").children.length, 0);
$("truth").value = "test-01";
change("truth");
assert.match(
  $("metrics").textContent,
  /Оценок без подтверждения другого прохода: 1/,
);
$("independent").checked = true;
change("independent");
assert.match($("metrics").textContent, /Верное имя: 1/);
$("candidates").children[1].click();
confirmed = false;
$("create").click();
assert.equal($("profile").options.length, 1);
confirmed = true;
$("add").click();
assert.equal($("references").children.length, 2);
const saved = JSON.parse(w.localStorage.getItem("anonymous-face-lab:abc:0"));
assert.equal(saved.history.length, 1);
assert.equal(saved.history[0].truth.hit, "test-01");
assert.deepEqual(saved.truth, {});
assert.equal($("create").disabled, true);
$("candidates").children[2].click();
$("create").click();
assert.equal($("profile").options.length, 2);
assert.match($("referenceStatus").textContent, /3 эталонов/);
$("filter").value = "no_embedding";
change("filter");
assert.equal($("prediction").textContent, "Нет вектора");
$("exact").checked = true;
change("exact");
assert.equal($("frame").hidden, false);
$("video").dispatchEvent(new w.Event("error"));
assert.equal($("mediaError").hidden, false);
$("export").click();
assert.equal(downloads, 1);
$("refVideo").value = "1";
change("refVideo");
assert.equal($("testVideo").value, "0");
assert.equal($("profile").options.length, 0);
$("refVideo").value = "0";
change("refVideo");
assert.equal($("profile").options.length, 2);
assert.deepEqual(errors, []);
dom.window.close();
console.log(
  "Anonymous lab: leakage guards, matching, enrollment, truth and UI checks passed",
);
if (process.argv[2]) {
  const path = require("node:path"),
    file = path.resolve(process.argv[2]);
  const actualErrors = [],
    actualVc = new VirtualConsole();
  actualVc.on("jsdomError", (e) => actualErrors.push(e.message));
  const actual = new JSDOM(fs.readFileSync(file, "utf8"), {
    url: "https://offline.invalid",
    runScripts: "dangerously",
    virtualConsole: actualVc,
    beforeParse(w) {
      w.HTMLMediaElement.prototype.pause = () => {};
    },
  });
  const d = actual.window.document,
    data = JSON.parse(d.getElementById("report-data").textContent);
  assert.equal(data.complete, true);
  for (const row of data.observations) {
    assert.equal("person_id" in row, false);
    assert.equal("outcomes" in row, false);
    for (const field of ["image", "context"])
      assert.equal(
        fs.existsSync(path.join(path.dirname(file), row[field])),
        true,
      );
    if (row.vector) {
      assert.equal(row.vector.length, 512);
      assert.ok(row.vector.every(Number.isFinite));
      assert.ok(Math.abs(Math.hypot(...row.vector) - 1) < 1e-5);
    }
  }
  for (const video of data.videos)
    assert.equal(
      fs.existsSync(path.join(path.dirname(file), video.video)),
      true,
    );
  assert.equal(d.getElementById("profile").options.length, 1);
  assert.notEqual(
    d.getElementById("testVideo").value,
    d.getElementById("refVideo").value,
  );
  assert.deepEqual(actualErrors, []);
  console.log(
    JSON.stringify({
      actual_report: true,
      videos: data.videos.length,
      observations: data.observations.length,
      test_summary: d.getElementById("counts").textContent,
    }),
  );
  actual.window.close();
}
