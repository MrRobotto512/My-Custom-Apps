// gemini_body.js - build a generateContent request body from two text files.
//   osascript -l JavaScript gemini_body.js system.txt user.txt  > body.json
// (JXA: runs on every Mac since 10.10, no python needed.)
ObjC.import('Foundation');
function readFile(p) {
  var s = $.NSString.stringWithContentsOfFileEncodingError(p, $.NSUTF8StringEncoding, $());
  if (s.isNil()) return '';
  return ObjC.unwrap(s);
}
function run(argv) {
  var sys = readFile(argv[0]);
  var user = readFile(argv[1]);
  var body = {
    system_instruction: { parts: [{ text: sys }] },
    contents: [{ role: 'user', parts: [{ text: user }] }],
    generationConfig: { temperature: 0.2, maxOutputTokens: 8192 }
  };
  return JSON.stringify(body);
}
