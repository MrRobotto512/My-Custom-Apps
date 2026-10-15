// report_json.js - write the JSON twin of a report.
//   osascript -l JavaScript report_json.js meta.txt findings.tsv ai.txt out.json
// meta.txt: key=value per line. findings.tsv: sev US cat US title US detail (US = 0x1f).
ObjC.import('Foundation');
var US = String.fromCharCode(31);
function readFile(p) {
  var s = $.NSString.stringWithContentsOfFileEncodingError(p, $.NSUTF8StringEncoding, $());
  if (s.isNil()) return '';
  return ObjC.unwrap(s);
}
function writeFile(p, text) {
  var s = $.NSString.alloc.initWithUTF8String(text);
  return s.writeToFileAtomicallyEncodingError(p, true, $.NSUTF8StringEncoding, $());
}
function run(argv) {
  var meta = {}, lines = readFile(argv[0]).split('\n');
  for (var i = 0; i < lines.length; i++) {
    var l = lines[i]; var k = l.indexOf('=');
    if (k > 0) meta[l.substring(0, k)] = l.substring(k + 1);
  }
  var findings = [], counts = { HIGH: 0, MED: 0, INFO: 0 };
  var fl = readFile(argv[1]).split('\n');
  for (var j = 0; j < fl.length; j++) {
    if (!fl[j]) continue;
    var f = fl[j].split(US);
    var sev = f[0] || 'INFO';
    findings.push({ severity: sev, category: f[1] || '', title: f[2] || '', detail: f[3] || '' });
    counts[sev] = (counts[sev] || 0) + 1;
  }
  var ai = argv[2] ? readFile(argv[2]) : '';
  var doc = {
    tool: meta.tool || '', version: meta.version || '', generated: meta.generated || '',
    host: meta.host || '', verdict: meta.verdict || '', counts: counts,
    system: {}, findings: findings, ai: { model: meta.ai_model || '', text: ai }
  };
  var skip = { tool: 1, version: 1, generated: 1, host: 1, verdict: 1, high: 1, med: 1, info: 1, ai_model: 1 };
  for (var key in meta) if (!skip[key]) doc.system[key] = meta[key];
  writeFile(argv[3], JSON.stringify(doc, null, 2) + '\n');
  return 'ok';
}
