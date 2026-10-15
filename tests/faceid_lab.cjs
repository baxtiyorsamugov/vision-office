const assert = require("node:assert/strict");
const fs = require("node:fs");
const { JSDOM, VirtualConsole } = require("jsdom");
const variants = ["base", "target", "retry", "compact"].map((id) => ({
  id,
  name: id,
}));
const unknown = { status: "unknown", reason: "usable", ms: 2 };
const matched = {
  ...unknown,
  status: "matched",
  person_id: "edu:1",
  name: "<img src=x onerror=alert(1)>",
};
const report = {
  created_at: "fixture",
  provenance: { gallery_sha256: "test" },
  complete: true,
  device: "cpu",
  gallery: { references: 1 },
  sample_fps: 5,
  threshold: 0.4,
  eduschool_threshold: 0.55,
  skipped_small: 10,
  variants,
  summary: variants.map((v) => ({
    ...v,
    count: 2,
    counts: { unknown: 2 },
    latency: { p50: 2, p95: 2 },
  })),
  videos: [
    { name: "one.mp4", video: "media/one.mp4", width: 1920, height: 1080 },
    { name: "empty.mp4", video: null, width: 1920, height: 1080 },
  ],
  observations: [
    {
      id: "0:0:0",
      video: 0,
      time: 1,
      frame: 25,
      image: "media/face.jpg",
      context: "media/frame.jpg",
      box: [0, 0, 60, 60],
      outcomes: {
        base: unknown,
        target: matched,
        retry: matched,
        compact: unknown,
      },
    },
    {
      id: "0:1:0",
      video: 0,
      time: 2,
      frame: 50,
      image: "media/face2.jpg",
      context: "media/frame2.jpg",
      box: [0, 0, 60, 60],
      outcomes: Object.fromEntries(variants.map((v) => [v.id, unknown])),
    },
  ],
};
const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push(e.message));
const html = fs
  .readFileSync("tools/templates/faceid_lab.html", "utf8")
  .replace(
    "__REPORT_JSON__",
    JSON.stringify(report).replaceAll("<", "\\u003c"),
  );
const dom = new JSDOM(html, {
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
const w = dom.window,
  $ = (id) => w.document.getElementById(id),
  change = (id) => $(id).dispatchEvent(new w.Event("change"));
assert.equal($("summary").children.length, 4);
assert.equal($("outcomes").children.length, 4);
assert.equal($("outcomes").querySelectorAll("img").length, 0);
assert.equal($("video").currentTime, 1);
assert.equal($("video").hidden, false);
const stage = $("video").parentElement;
Object.defineProperty(stage, "clientWidth", { value: 960 });
Object.defineProperty(stage, "clientHeight", { value: 540 });
$("video").dispatchEvent(new w.Event("timeupdate"));
assert.equal($("overlays").children.length, 1);
$("playbackVariant").value = "target";
change("playbackVariant");
assert.equal($("overlays").textContent, matched.name);
assert.equal($("overlays").querySelectorAll("img").length, 0);
$("video").currentTime = 1.5;
$("video").dispatchEvent(new w.Event("timeupdate"));
assert.equal($("overlays").children.length, 0);
$("exact").checked = true;
change("exact");
assert.equal($("context").hidden, false);
$("next").click();
assert.equal($("video").currentTime, 2);
assert.equal($("next").disabled, true);
$("prev").click();
const select = $("outcomes").querySelector("select");
select.value = "wrong";
select.dispatchEvent(new w.Event("change"));
assert.match($("reviewStats").textContent, /Ошибка: 1/);
$("truth").value = "Known person";
$("truth").dispatchEvent(new w.Event("input"));
assert.match(w.localStorage.getItem(w.localStorage.key(0)), /Known person/);
$("filter").value = "new";
change("filter");
assert.equal($("position").textContent, "1 / 1");
$("search").value = "does not exist";
$("search").dispatchEvent(new w.Event("input"));
assert.equal($("empty").hidden, false);
$("search").value = "";
$("search").dispatchEvent(new w.Event("input"));
$("video").dispatchEvent(new w.Event("error"));
assert.equal($("mediaError").hidden, false);
$("videoSelect").value = "1";
change("videoSelect");
assert.equal($("inspection").hidden, true);
$("export").click();
assert.deepEqual(errors, []);
dom.window.close();
console.log("FaceID lab UI checks passed");
