// gemini_text.js - print the text of a generateContent response file.
//   osascript -l JavaScript gemini_text.js response.json
ObjC.import('Foundation');
function readFile(p) {
  var s = $.NSString.stringWithContentsOfFileEncodingError(p, $.NSUTF8StringEncoding, $());
  if (s.isNil()) return '';
  return ObjC.unwrap(s);
}
function run(argv) {
  var raw = readFile(argv[0]);
  var j;
  try { j = JSON.parse(raw); } catch (e) { return ''; }
  if (!j || !j.candidates || !j.candidates.length) return '';
  var parts = (j.candidates[0].content && j.candidates[0].content.parts) || [];
  var out = [];
  for (var i = 0; i < parts.length; i++) if (parts[i].text) out.push(parts[i].text);
  return out.join('\n').replace(/\s+$/, '');
}
