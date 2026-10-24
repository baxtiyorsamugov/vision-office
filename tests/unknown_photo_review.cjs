const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM, VirtualConsole} = require('jsdom');
const report = {
  version:1, snapshot_id:'unknown-fixture', dump_sha256:'dump', review_scope:'unknown', skipped:{events:0},
  people:[
    {id:'employee:a', full_name:'Person A',person_type:'employee',active:true,references:[{id:'ref',image:'a.jpg'}]},
    {id:'employee:b', full_name:'Person B',person_type:'employee',active:true,references:[]}
  ],
  events:Array.from({length:3},(_,i)=>({id:'event-'+i,image:'event.jpg',sha256:'hash-'+i,
    camera:'camera',at:'2026-10-15T10:00:00Z',kind:'entry',width:300-i,height:300-i,
    prediction_type:'unknown',prediction_name:null,prediction_person_id:null}))
};
function htmlFor(data) {
  return fs.readFileSync('tools/templates/photo_review.html','utf8')
    .replace('__STYLE__',fs.readFileSync('tools/templates/photo_review.css','utf8'))
    .replace('__SCRIPT__',fs.readFileSync('tools/templates/photo_review.js','utf8'))
    .replace('__REPORT_JSON__',JSON.stringify(data));
}
const errors=[]; const vc=new VirtualConsole();vc.on('jsdomError',e=>errors.push(e.message));
const oldKey='vision-office-photo-review-v1:known-fixture';
const dom=new JSDOM(htmlFor(report),{runScripts:'dangerously',url:'https://offline.invalid/',virtualConsole:vc,
  beforeParse(w){w.localStorage.setItem(oldKey,'existing-known-decisions');}});
const w=dom.window,$=id=>w.document.getElementById(id),change=id=>$(id).dispatchEvent(new w.Event('change'));
const choose=id=>w.document.querySelector(`[data-person="${id}"]`).click();
const load=id=>$(id).dispatchEvent(new w.Event('load'));
assert.equal($('reviewMode').value,'manual');
assert.equal($('source').value,'unknown');
assert.equal($('source').disabled,true);
assert.equal($('sort').value,'largest');
assert.equal($('queue').children.length,3);
assert.equal($('personName').textContent,'Человек не выбран');
assert.equal($('confirmLabel').textContent,'Да');
assert.equal($('unsureLabel').textContent,'Не уверен');
assert.equal($('confirm').disabled,true);
assert.equal($('reject').disabled,true);
assert.equal($('unsure').disabled,false);
choose('employee:a');
load('frameImage');
$('verified').checked=true;change('verified');
assert.equal($('confirm').disabled,true,'unloaded reference cannot confirm');
load('referenceImage');
assert.equal($('confirm').disabled,false);
assert.equal($('journalReference').hidden,false);
$('confirm').click();
let session=w.photoReview.exportSession();
assert.equal(session.decisions['event-0'].person_id,'employee:a');
assert.equal(session.decisions['event-0'].reference_approved,true);
assert.equal(session.decisions['event-0'].review_method,'manual');
assert.equal(session.applied,false);
assert.equal($('personName').textContent,'Человек не выбран');
assert.equal($('verified').checked,false);
assert.equal($('confirm').disabled,true);
assert.equal($('rejectReason').value,'');
choose('employee:b');load('frameImage');
$('verified').checked=true;change('verified');
assert.equal($('confirm').disabled,true,'missing reference cannot confirm');
$('unsure').click();
$('rejectReason').value='quality';change('rejectReason');
assert.equal($('reject').disabled,false);
$('reject').click();
session=w.photoReview.exportSession();
assert.equal(session.decisions['event-1'].status,'unsure');
assert.equal(session.decisions['event-2'].reason,'quality');
assert.equal(w.photoReview.validateSession(session).size,3);
assert.throws(()=>w.photoReview.validateSession({...session,snapshot_id:'known-fixture'}));
assert.equal(w.localStorage.getItem(oldKey),'existing-known-decisions');
$('undo').click();
assert.equal(w.photoReview.exportSession().decisions['event-2'],undefined);
assert.equal($('source').value,'unknown');
assert.deepEqual(errors,[]);
dom.window.close();
console.log('Unknown review: selection, image guards, decisions, undo, storage isolation passed.');
if(process.argv[2]) {
  const html=fs.readFileSync(process.argv[2],'utf8');
  const parsed=new JSDOM(html);
  const actual=JSON.parse(parsed.window.document.getElementById('report').textContent);
  assert.equal(actual.review_scope,'unknown');
  assert.equal(actual.events.length,20902);
  assert(actual.events.every(e=>e.prediction_type==='unknown'));
  assert(actual.events.every(e=>e.image.startsWith('../photo-review-20261016/media/')));
  const source=JSON.parse(fs.readFileSync('data/exports/photo-review-20261016/manifest.json','utf8'));
  assert.notEqual(actual.snapshot_id,source.snapshot_id);
  parsed.window.close();
  console.log('Actual unknown bundle: 20902 unknown-only events and separate snapshot verified.');
}
