// Run with node; jsdom must be installed in NODE_PATH or the project environment.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { JSDOM, VirtualConsole } = require("jsdom");
const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (error) => errors.push(error.message));
const sample = (time, frame) => ({
  time,
  frame,
  image: `media/${frame}.jpg`,
  detection_ms: 10,
  faces: [{ box: [100, 100, 200, 200], status: "unknown" }],
});
const video = {
  name: "fixture.mp4",
  video: "media/fixture.mp4",
  width: 960,
  height: 540,
  duration: 2,
  face_device: "cpu",
  counts: { unknown: 3 },
  samples: [sample(0, 0), sample(0.2, 5), sample(0.4, 10)],
};
const report = {
  created_at: "2026-01-01T00:00:00Z",
  sample_fps: 5,
  device: "cpu",
  videos: [video, { ...video, name: "second.mp4" }],
  gallery: { people: 0, references: 0 },
};
const template = fs.readFileSync("tools/templates/video_report.html", "utf8");
let strokes = 0,
  callback,
  canceled = 0;
function makeDom(nativeCallbacks = true) {
  return new JSDOM(
    template.replace("__REPORT_JSON__", JSON.stringify(report)),
    {
      url: "https://offline.invalid",
      runScripts: "dangerously",
      virtualConsole: vc,
      beforeParse(w) {
        w.ResizeObserver = class {
          observe() {}
        };
        w.requestAnimationFrame = (cb) => {
          callback = cb;
          return 1;
        };
        w.cancelAnimationFrame = () => {
          callback = null;
          canceled++;
        };
        const proto = w.HTMLMediaElement.prototype;
        Object.defineProperty(proto, "paused", {
          get() {
            return this._paused !== false;
          },
        });
        Object.defineProperty(proto, "readyState", {
          get() {
            return 2;
          },
        });
        Object.defineProperty(proto, "seeking", {
          get() {
            return Boolean(this._seeking);
          },
        });
        proto.pause = function () {
          this._paused = true;
          this.dispatchEvent(new w.Event("pause"));
        };
        proto.play = function () {
          this._paused = false;
          this.dispatchEvent(new w.Event("play"));
          return Promise.resolve();
        };
        if (nativeCallbacks) {
          proto.requestVideoFrameCallback = (cb) => {
            callback = cb;
            return 1;
          };
          proto.cancelVideoFrameCallback = () => {
            callback = null;
            canceled++;
          };
        }
        w.HTMLCanvasElement.prototype.getContext = () => ({
        setTransform() {},
        clearRect() {},
          strokeRect() {
            strokes++;
          },
          fillRect() {},
          fillText() {},
          measureText(text) {
            return { width: text.length * 6 };
          },
        });
      },
    },
  );
}
for (const native of [true, false]) {
  const dom = makeDom(native),
    w = dom.window,
    $ = (id) => w.document.getElementById(id);
  const event = (type) => new w.Event(type);
  assert.equal($("video").hidden, false);
  assert.equal($("frame-image").hidden, true);
  assert.equal($("overlay").hidden, false);
  $("video").play();
  assert.ok(callback);
  assert.equal(w.document.querySelector("#faces select").disabled, true);
  const before = strokes;
  $("video").currentTime = 0.25;
  callback(0, { mediaTime: 0.25 });
  assert.equal($("seek").value, "1");
  assert.ok(strokes > before);
  $("video")._seeking = true;
  const beforeSeek = strokes;
  $("video").dispatchEvent(event("seeking"));
  assert.equal(strokes, beforeSeek);
  assert.equal(callback, null);
  $("video")._seeking = false;
  $("video").currentTime = 0.05;
  $("video").dispatchEvent(event("seeked"));
  assert.equal($("seek").value, "0");
  $("video").pause();
  assert.equal(callback, null);
  assert.equal(w.document.querySelector("#faces select").disabled, false);
  const stale = strokes;
  $("video").currentTime = 1.5;
  $("video").dispatchEvent(event("timeupdate"));
  assert.equal(
    strokes,
    stale,
    "Do not keep a box over a long gap without observations",
  );
  $("video").currentTime = 0.25;
  $("analysis-mode").click();
  assert.equal($("video").hidden, true);
  assert.equal($("seek").value, "1");
  $("next").click();
  assert.equal($("seek").value, "2");
  $("previous").click();
  assert.equal($("seek").value, "1");
  const select = w.document.querySelector("#faces select");
  select.value = "unclear";
  select.dispatchEvent(event("change"));
  assert.match($("review-counts").textContent, /1/);
  $("video-mode").click();
  assert.equal($("video").currentTime, 0.2);
  $("speed").value = "2";
  $("speed").dispatchEvent(event("change"));
  assert.equal($("video").playbackRate, 2);
  $("video").play();
  $("recording").value = "1";
  $("recording").dispatchEvent(event("change"));
  assert.equal($("seek").value, "0");
  assert.equal(callback, null);
  assert.equal($("video").playbackRate, 2);
  dom.window.close();
}
assert.ok(canceled > 0);
const qualityTemplate = fs.readFileSync("tools/templates/reference_quality.html", "utf8");
const qualityReport = {
  created_at: report.created_at, summary: { checked: 62, review: 61 },
  coverage: { active: { student: 62 }, without_reference: { student: 0 } },
  photos: Array.from({ length: 62 }, (_, i) => ({
    id: String(i), person_id: String(i), name: i === 0 ? "<b>Fixture</b>" : `Person ${i}`,
    category: "student", flags: i === 61 ? [] : ["low_detail"],
  })),
};
const qualityDom = new JSDOM(qualityTemplate.replace("__REPORT_JSON__", JSON.stringify(qualityReport)), {
  runScripts: "dangerously", virtualConsole: vc,
});
const q = id => qualityDom.window.document.getElementById(id);
assert.equal(q("rows").children.length, 50);
assert.equal(q("rows").querySelector("b"), null);
q("next").click();
assert.equal(q("rows").children.length, 11);
q("status").value = "clear";
q("status").dispatchEvent(new qualityDom.window.Event("change"));
assert.equal(q("rows").children.length, 1);
assert.equal(q("page").textContent, "1 / 1");
q("search").value = "not found";
q("search").dispatchEvent(new qualityDom.window.Event("input"));
assert.equal(q("rows").children.length, 0);
assert.equal(q("next").disabled, true);
qualityDom.window.close();
assert.deepEqual(errors, []);
console.log(
  "PASS: video/frame modes, both callback paths, seek, pause, stale boxes, review labels, speed and recording changes",
);
