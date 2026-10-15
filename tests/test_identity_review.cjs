const fs = require("node:fs");
const assert = require("node:assert/strict");
const { JSDOM, VirtualConsole } = require("jsdom");
const report = {
  session_sha256: "fixture",
  source_sha256: "source",
  gap_seconds: 2,
  threshold: 0.55,
  sample_fps: 5,
  videos: [
    { name: "a.mp4", video: "media/a.mp4", width: 1920, height: 1080 },
    { name: "b.mp4", video: "media/b.mp4", width: 1920, height: 1080 },
  ],
  galleries: [
    {
      key: "0",
      source_video: 0,
      profiles: [
        {
          id: "test-01",
          refs: ["ref"],
          same_clip: 1,
          other_clips: 0,
          ambiguous: 0,
        },
      ],
      counts: [{ matched: 1, excluded: 1 }, { unknown: 1 }],
      control: { exact_reference_ok: 1 },
    },
  ],
  observations: [
    {
      id: "ref",
      video: 0,
      time: 1,
      frame: 25,
      box: [0, 0, 80, 80],
      image: "media/ref.jpg",
      context: "media/frame.jpg",
      outcomes: { 0: { status: "excluded" } },
    },
    {
      id: "hit",
      video: 0,
      time: 5,
      frame: 125,
      box: [0, 0, 80, 80],
      image: "media/hit.jpg",
      context: "media/frame.jpg",
      outcomes: {
        0: { status: "matched", person: "test-01", reference: "ref" },
      },
    },
    {
      id: "unknown",
      video: 1,
      time: 0,
      frame: 0,
      box: [0, 0, 80, 80],
      image: "media/unknown.jpg",
      context: "media/frame.jpg",
      outcomes: { 0: { status: "unknown" } },
    },
  ],
};
const errors = [],
  vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push(e.message));
function dom(html) {
  return new JSDOM(html, {
    url: "https://offline.invalid",
    runScripts: "dangerously",
    virtualConsole: vc,
    beforeParse(w) {
      w.HTMLMediaElement.prototype.pause = () => {};
      w.URL.createObjectURL = () => "blob:test";
      w.URL.revokeObjectURL = () => {};
      w.HTMLAnchorElement.prototype.click = () => {};
    },
  });
}
const fixture = dom(
    fs
      .readFileSync("tools/templates/test_identity_review.html", "utf8")
      .replace("__REPORT_JSON__", JSON.stringify(report)),
  ),
  w = fixture.window,
  $ = (id) => w.document.getElementById(id),
  change = (id) => $(id).dispatchEvent(new w.Event("change"));
assert.equal($("position").textContent, "1 / 1");
assert.equal($("prediction").textContent, "Тест-01?");
assert.equal($("video").currentTime, 5);
assert.equal($("people").children.length, 1);
$("mark").value = "correct";
change("mark");
assert.match($("reviewStats").textContent, /Один человек: 1/);
$("exact").checked = true;
change("exact");
assert.equal($("frame").hidden, false);
$("filter").value = "excluded";
change("filter");
assert.equal($("mark").disabled, true);
$("videoSelect").value = "1";
change("videoSelect");
assert.equal($("empty").hidden, false);
$("filter").value = "all";
change("filter");
assert.equal($("prediction").textContent, "Неизвестен");
assert.equal($("mark").disabled, true);
assert.equal($("reference").hidden, true);
$("video").dispatchEvent(new w.Event("error"));
assert.equal($("mediaError").hidden, false);
$("export").click();
assert.deepEqual(errors, []);
fixture.window.close();
if (process.argv[2]) {
  const path = require("node:path"),
    file = path.resolve(process.argv[2]),
    actual = dom(fs.readFileSync(file, "utf8"));
  const d = actual.window.document,
    r = JSON.parse(d.getElementById("report-data").textContent);
  const rows = new Map(r.observations.map((x) => [x.id, x]));
  for (const gallery of r.galleries) {
    const refs = gallery.profiles.flatMap((p) =>
      p.refs.map((id) => rows.get(id)),
    );
    for (const row of r.observations) {
      const result = row.outcomes[gallery.key];
      const excluded =
        row.video === gallery.source_video &&
        refs.some(
          (ref) => Math.abs(ref.time - row.time) <= r.gap_seconds + 1e-8,
        );
      assert.equal(result.status === "excluded", excluded);
      if (result.status === "matched") assert.ok(rows.has(result.reference));
    }
  }
  for (const row of r.observations)
    for (const key of ["image", "context"])
      assert.ok(fs.existsSync(path.join(path.dirname(file), row[key])));
  assert.deepEqual(errors, []);
  console.log(
    JSON.stringify({
      actual_report: true,
      galleries: r.galleries.length,
      observations: r.observations.length,
    }),
  );
  actual.window.close();
}
console.log(
  "Test-ID viewer: filters, labels, export, media fallback and leakage checks passed",
);
